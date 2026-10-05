"""SOC 估算组件。

``SocEstimator`` 是 BMS 校准结果的下游：每次估算都取当前代校准作为基准，
再叠上这一代锚点之后的库仑净增量（连续累计、从不清零，口径见
:mod:`batterystore.soc.ledger`）。校准一换代，下一轮估算立刻按新基准算，
不会继续沿用旧偏移。

对外只发布一份 SOC：任何改变电量的动作（周期推进、充放电、校准、现场
标定）都走 :meth:`publish`，在同一把锁里算值、存值并把值同步给各簇；
页面、调度等所有消费者都通过 :meth:`current` 读这同一份带修订号的值，
不会出现"页面一个数、调度另一个数"。

累计与校准是两个来源：事件按发生时刻定胜负，更晚的为准；早于当前锚点
的乱序事件一律不采纳，只在冲突台账里留痕。
"""

from __future__ import annotations

import threading
import time

from ..config import SocLimits
from .conflict import SourceConflictLog
from .coulomb import CoulombCounter
from .estimate import SocEstimate, estimate_soc
from .history import SocHistory
from .ledger import CalibrationLedger
from .snapshot import SocSnapshot


def _now_ts() -> float:
    """当前墙钟秒，仅在调用方没给仿真时刻时使用。"""

    return time.time()


class SocEstimator:
    """SOC 估算组件。"""

    def __init__(self, limits: SocLimits, calibration) -> None:
        self._limits = limits
        self._calibration = calibration
        self._counter = CoulombCounter(limits.coulomb_efficiency)
        self._history = SocHistory()
        self._ledger = CalibrationLedger(limits.drift_tolerance_pct, limits.initial_soc_pct)
        self._conflicts = SourceConflictLog()
        self._reported_pct = float(limits.initial_soc_pct)
        self._revision = 0
        self._published: SocEstimate | None = None
        self._published_ts = 0.0
        self._lock = threading.RLock()

    @property
    def limits(self) -> SocLimits:
        return self._limits

    @property
    def counter(self) -> CoulombCounter:
        return self._counter

    @property
    def history(self) -> SocHistory:
        return self._history

    @property
    def conflicts(self) -> SourceConflictLog:
        return self._conflicts

    def accumulate(self, current_a: float, dt: float, now: float | None = None) -> float:
        """按电流与步长累计库仑量，返回本次净安时。

        时刻早于当前校准锚点的累计属于乱序晚到事件：更晚的校准已经定了
        新基准，这次累计不得回灌，记一条冲突并返回 0（不改变任何账）。
        """

        with self._lock:
            stamp = float(now) if now is not None else _now_ts()
            anchor_ts = self._ledger.anchor()["stamped_at"]
            if stamp < anchor_ts:
                self._conflicts.note_rejected(
                    kind="accumulation",
                    ts=stamp,
                    cutoff_ts=anchor_ts,
                    winner="calibration",
                    detail={"current_a": float(current_a), "dt_s": float(dt)},
                )
                return 0.0
            return self._counter.through(current_a, dt)

    def calibration_applied(self, now: float) -> SocEstimate:
        """校准登记后调用：按新一代开锚点并立即按新基准发布 SOC。"""

        with self._lock:
            self._adopt_calibration(now)
            return self.publish(now)

    def _adopt_calibration(self, now: float) -> dict:
        """若校准代际比台账新，就按新一代开锚点；旧代际则记乱序冲突。"""

        latest = self._calibration.latest(now)
        epoch = int(latest["epoch"])
        current_epoch = self._ledger.current_epoch()
        if epoch < current_epoch:
            anchor = self._ledger.anchor()
            self._conflicts.note_rejected(
                kind="calibration",
                ts=float(latest.get("stamped_at", now)),
                cutoff_ts=anchor["stamped_at"],
                winner="calibration",
                detail={"epoch": epoch, "current_epoch": current_epoch, "soc_pct": latest["soc_pct"]},
            )
            return anchor
        if epoch > current_epoch:
            return self._ledger.open_epoch(
                epoch,
                float(latest["soc_pct"]),
                self._counter.net_ah(),
                float(latest.get("stamped_at", now)),
                str(latest.get("source", "manual")),
            )
        return self._ledger.anchor()

    def value(self, now: float) -> SocEstimate:
        """按当前代校准估算当前 SOC（只算，不改变对外发布值）。"""

        with self._lock:
            return self._estimate(now)

    def publish(self, now: float) -> SocEstimate:
        """重估并把结果落为唯一对外值，返回本次估算。"""

        with self._lock:
            estimate = self._estimate(now)
            self._revision += 1
            self._published = estimate
            self._published_ts = float(now)
            self._history.record(estimate.soc_pct, self._reported_pct, now, estimate.calibration_epoch)
            return estimate

    def current(self) -> dict:
        """返回当前对外发布的那份 SOC（所有消费者的唯一读数）。"""

        with self._lock:
            if self._published is None:
                raise RuntimeError("SOC 尚未发布")
            return {
                **self._published.as_dict(),
                "revision": self._revision,
                "published_at": round(self._published_ts, 3),
            }

    def revision(self) -> int:
        """返回对外值的修订号，每次发布单调加一，供消费者核对同一份值。"""

        with self._lock:
            return self._revision

    def reported_pct(self) -> float:
        """返回调度最近一次采用的 SOC。"""

        with self._lock:
            return self._reported_pct

    def report(self, soc_pct: float) -> float:
        """更新调度采用值，返回被替换掉的旧值。"""

        with self._lock:
            previous = self._reported_pct
            self._reported_pct = float(soc_pct)
            return previous

    def drift_pct(self) -> float:
        """返回窗口内最近一次发布的偏差绝对值。"""

        with self._lock:
            return self._history.drift_pct()

    def deviation_state(self) -> dict:
        """返回对外值与调度采用值之间的实时偏差及是否越限。

        偏差每次都按"当前对外值 - 当前采用值"现算：采用值可能在两次发布
        之间被调度刷新，不能用发布时刻固化的旧偏差决定要不要阻止放电。
        """

        with self._lock:
            if self._published is None:
                soc_pct = self._reported_pct
                epoch = self._ledger.current_epoch()
            else:
                soc_pct = self._published.soc_pct
                epoch = self._published.calibration_epoch
            drift = float(soc_pct) - self._reported_pct
            return {
                "soc_pct": round(float(soc_pct), 3),
                "reported_pct": round(self._reported_pct, 3),
                "drift_pct": round(drift, 3),
                "tolerance_pct": round(self._limits.drift_tolerance_pct, 3),
                "exceeded": abs(drift) > self._limits.drift_tolerance_pct,
                "calibration_epoch": epoch,
                "revision": self._revision,
            }

    def reconciliation(self, limit: int = 20) -> dict:
        """返回跨校准段电量对账。"""

        with self._lock:
            return self._ledger.reconciliation(
                self._counter.net_ah(), self._limits.capacity_ah, limit=limit
            )

    def drain_conflicts(self) -> list[dict]:
        """取出并清空待上报的来源冲突记录，供平台写审计。"""

        with self._lock:
            pending = self._conflicts.entries(limit=self._conflicts.count())
            return list(reversed(pending))

    def snapshot(self, now: float) -> SocSnapshot:
        """返回 SOC 状态快照（读当前对外值，不制造新采样）。"""

        with self._lock:
            if self._published is None:
                self.publish(now)
            assert self._published is not None
            return SocSnapshot(
                current=self.current(),
                calibration=self._calibration.latest(now),
                coulomb={
                    "charged_ah": round(self._counter.charged_ah(), 6),
                    "discharged_ah": round(self._counter.discharged_ah(), 6),
                    "net_ah": round(self._counter.net_ah(), 6),
                    "intervals": self._counter.intervals(),
                },
                margin_pct=self._limits.drift_tolerance_pct - abs(self._published.soc_pct - self._reported_pct),
                samples=self._history.count(),
                reconciliation=self.reconciliation(),
                conflicts=self._conflicts.entries(),
            )

    def _estimate(self, now: float) -> SocEstimate:
        latest = self._calibration.latest(now)
        self._adopt_calibration(now)
        anchor = self._ledger.anchor(int(latest["epoch"]))
        net_since_anchor = self._counter.net_ah() - float(anchor["counter_ah"])
        return estimate_soc(latest, net_since_anchor, self._limits, self._reported_pct)
