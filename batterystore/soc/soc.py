"""SOC 估算组件。

``SocEstimator`` 是 BMS 校准结果与库仑累计的唯一收口：

* **校准即换基准**：每次校准封存当前段、以新校准值为零点另开新段，下一次
  发布立刻按新基准算，绝不沿用校准前的偏移（实际剩 30% 还显示 45% 的问题
  就是旧偏移没被换掉）；
* **代际可查**：每次发布的 SOC 都带上所用校准代际，页面、调度、审计看到的
  是同一个带代际的值；
* **偏差明示**：两种偏差都显式带出并告警——累计推算相对最近权威基准漂出
  容差（``drift_exceeded``），以及新一代校准相对校准前推算值的跳变量超过
  容差（``correction_exceeded``，即"账面 45% 实测 30%"那一抓）；
* **单一读数**：估算结果在锁内一次性发布成不可变快照，所有消费者
  （控制页面、调度、快照接口）都读同一份已发布值，不会各算各的；
* **来源裁决**：累计与校准是两个来源，冲突时按"更晚发生的那次为准"裁决
  并在 :class:`~batterystore.soc.sources.SourceLedger` 留痕。

跨校准段的口径选择（清零重起而非保留累计加修正量）记录在
:mod:`batterystore.soc.segments`。
"""

from __future__ import annotations

import threading

from ..config import SocLimits
from .estimate import SocEstimate, estimate_soc
from .history import SocHistory
from .segments import SegmentLedger
from .snapshot import SocSnapshot
from .sources import CALIBRATION_SOURCE, COULOMB_SOURCE, SourceLedger

_PUBLISHED_KEYS = (
    "soc_pct",
    "calibration_epoch",
    "base_soc_pct",
    "delta_pct",
    "stale",
    "drift_pct",
    "drift_tolerance_pct",
    "drift_exceeded",
    "correction_pct",
    "correction_exceeded",
)

DRIFT_ALARM = "drift_exceeded"
CORRECTION_ALARM = "correction_exceeded"


class SocEstimator:
    """SOC 估算组件：单一口径、单一发布值。"""

    def __init__(self, limits: SocLimits, calibration, alarm=None) -> None:
        self._limits = limits
        self._calibration = calibration
        self._alarm = alarm
        self._segments = SegmentLedger(limits.coulomb_efficiency)
        self._sources = SourceLedger()
        self._history = SocHistory()
        self._anchor_pct = float(limits.initial_soc_pct)
        self._published: dict | None = None
        self._drift_alarm_active = False
        self._lock = threading.RLock()

    @property
    def limits(self) -> SocLimits:
        return self._limits

    @property
    def segments(self) -> SegmentLedger:
        return self._segments

    @property
    def sources(self) -> SourceLedger:
        return self._sources

    @property
    def history(self) -> SocHistory:
        return self._history

    def begin_epoch(self, epoch: int, base_soc_pct: float, stamped_at: float, source: str) -> None:
        """以一代校准为零点开段（启动首代校准走这里，不封存任何段）。

        同时原子发布这代基准作为首个统一读数，保证平台一上电页面与调度
        就有同一个值可读，而不是一边读到空、另一边触发惰性发布。
        """

        with self._lock:
            self._segments.open(epoch, base_soc_pct, stamped_at, source)
            self._sources.commit(CALIBRATION_SOURCE, stamped_at, {"epoch": epoch, "soc_pct": base_soc_pct})
            self._anchor_pct = float(base_soc_pct)
            self._publish(stamped_at)

    def check_calibration(self, soc_pct: float, stamped_at: float) -> dict | None:
        """裁决一版校准会不会因"更早发生"而判负；判负时直接落冲突记录。

        上层必须在向 BMS 登记校准**之前**调用：判负就不推进校准代际，
        保证估算侧代际与 BMS 代际不分叉。返回 None 表示可以登记。
        """

        with self._lock:
            verdict = self._sources.verdict(CALIBRATION_SOURCE, stamped_at)
            if not verdict["stale"]:
                return None
            if verdict["conflict"]:
                return self._sources.record_rejection(
                    CALIBRATION_SOURCE, stamped_at, {"soc_pct": float(soc_pct)}
                )
            # 同源（更早的一代校准）迟到：不落冲突，但同样挡住不登记。
            return {
                "winner": CALIBRATION_SOURCE,
                "rejected_source": CALIBRATION_SOURCE,
                "rejected_ts": round(float(stamped_at), 3),
                "held_source": CALIBRATION_SOURCE,
                "held_ts": verdict["previous"]["ts"],
                "same_source": True,
            }

    def accumulate(self, current_a: float, dt: float, now: float | None = None) -> float:
        """在当前校准段内累计库仑量，返回本次净安时并发布。

        先裁决来源先后再动账：这次累计若不早于最近一次生效写入则入账；
        若属于迟到样本（时刻更早），判负丢弃、已发布值不回退，只留冲突记录。
        """

        with self._lock:
            ts = self._resolve_ts(now, dt)
            verdict = self._sources.verdict(COULOMB_SOURCE, ts)
            if verdict["stale"]:
                if verdict["conflict"]:
                    # 跨来源迟到样本：判负丢弃并留冲突记录。
                    self._sources.record_rejection(
                        COULOMB_SOURCE, ts, {"current_a": float(current_a), "dt": float(dt)}
                    )
                # 同源遥测乱序同样丢弃，但不算两个基准打架。
                return 0.0
            delta_ah = self._segments.accumulate(current_a, dt)
            self._sources.commit(
                COULOMB_SOURCE,
                ts,
                {"delta_ah": round(delta_ah, 6), "net_ah": round(self._segments.net_ah(), 6)},
            )
            self._publish(ts)
            return delta_ah

    def _resolve_ts(self, now: float | None, dt: float) -> float:
        if now is not None:
            return float(now)
        last = self._sources.last_ts()
        return float(last) if last is not None else float(dt)

    def apply_calibration(self, record: dict, now: float, previous_soc_pct: float) -> SocEstimate:
        """登记一代新校准：封存旧段 → 开新段 → 立即重估发布。

        ``previous_soc_pct`` 是校准前的已发布估算值，用于计算这版校准的
        带符号跳变量（新基准 − 旧推算），跳变超容差即发校准修正告警。
        调用方需先用 :meth:`check_calibration` 挡住迟到校准。
        """

        with self._lock:
            stamped_at = float(record["stamped_at"])
            self._segments.seal(self._limits.capacity_ah, float(record["soc_pct"]))
            self._segments.open(
                record["epoch"], record["soc_pct"], stamped_at, record.get("source", "manual")
            )
            self._sources.commit(
                CALIBRATION_SOURCE,
                stamped_at,
                {"epoch": record["epoch"], "soc_pct": record["soc_pct"]},
            )
            self._anchor_pct = float(record["soc_pct"])
            self._publish(now)
            corrected = self.current(now).with_correction(previous_soc_pct)
            self._store_published(corrected, now)
            if corrected.correction_exceeded and self._alarm is not None:
                self._alarm(CORRECTION_ALARM, corrected, dict(self._published))
            return self.current(now)

    def _estimate_locked(self, now: float) -> SocEstimate:
        latest = self._calibration.latest(now)
        return estimate_soc(latest, self._segments.net_ah(), self._limits, self._anchor_pct)

    def _publish(self, now: float) -> dict:
        """在锁内完成一次估算并原子替换已发布值。"""

        estimate = self._estimate_locked(now)
        self._history.record(estimate.soc_pct, self._anchor_pct, now, estimate.calibration_epoch)
        return self._store_published(estimate, now)

    def _store_published(self, estimate: SocEstimate, now: float) -> dict:
        """把给定估算原子落为唯一已发布快照，并处理漂移边沿告警。"""

        data = estimate.as_dict()
        published = {
            **{key: data[key] for key in _PUBLISHED_KEYS},
            "published_at": round(float(now), 3),
            "last_source": self._sources.last_source(),
        }
        self._published = published
        if estimate.drift_exceeded and not self._drift_alarm_active:
            self._drift_alarm_active = True
            if self._alarm is not None:
                self._alarm(DRIFT_ALARM, estimate, dict(published))
        elif not estimate.drift_exceeded:
            self._drift_alarm_active = False
        return dict(published)

    @staticmethod
    def _from_published(data: dict) -> SocEstimate:
        return SocEstimate(**{key: data[key] for key in _PUBLISHED_KEYS})

    def current(self, now: float | None = None) -> SocEstimate:
        """返回当前已发布 SOC（所有消费者的统一读数）。

        尚无发布值时按最新校准发布一次；之后只读快照，绝不为不同消费者
        各算一遍，保证页面与调度拿到的是同一个值。
        """

        with self._lock:
            if self._published is None:
                self._publish(float(now) if now is not None else 0.0)
            return self._from_published(dict(self._published))

    def published(self) -> dict:
        """返回最近一次发布快照（含校准代际与时间戳），供各消费者直接引用。"""

        with self._lock:
            if self._published is None:
                return {}
            return dict(self._published)

    def value(self, now: float) -> SocEstimate:
        """按最新状态重新估算并发布（控制周期推进时调用）。"""

        with self._lock:
            self._publish(now)
            return self.current(now)

    def drift_pct(self) -> float:
        """返回窗口内的漂移量。"""

        return self._history.drift_pct()

    def drift_exceeded(self) -> bool:
        """返回当前漂移是否仍在容差之外。"""

        with self._lock:
            return bool(self._published and self._published["drift_exceeded"])

    def correction_exceeded(self) -> bool:
        """返回最近一代校准的跳变量是否超过容差。"""

        with self._lock:
            return bool(self._published and self._published["correction_exceeded"])

    def reconcile_segments(self) -> list[dict]:
        """跨校准段电量对账（口径一见 :mod:`batterystore.soc.segments`）。"""

        with self._lock:
            current = self._published["soc_pct"] if self._published else self._anchor_pct
        return self._segments.reconcile(self._limits.capacity_ah, current)

    def reset(self) -> dict:
        """清零当前开段累计（已封存段保留），返回清零前的值。"""

        with self._lock:
            return self._segments.reset_open()

    def snapshot(self, now: float) -> SocSnapshot:
        """返回 SOC 状态快照。"""

        with self._lock:
            estimate = self.current(now)
            calibration = self._calibration.latest(now)
            coulomb = {
                "charged_ah": round(self._segments.charged_ah(), 6),
                "discharged_ah": round(self._segments.discharged_ah(), 6),
                "net_ah": round(self._segments.net_ah(), 6),
                "intervals": self._segments.intervals(),
            }
            margin_pct = self._limits.drift_tolerance_pct - abs(estimate.drift_pct)
            samples = self._history.count()
            segments = self._segments.segments()
            conflicts = self._sources.conflicts()
            published = dict(self._published or estimate.as_dict())
            reconciliation = self.reconcile_segments()
        return SocSnapshot(
            estimate=estimate.as_dict(),
            calibration=calibration,
            coulomb=coulomb,
            margin_pct=margin_pct,
            samples=samples,
            published=published,
            segments=segments,
            conflicts=conflicts,
            reconciliation=reconciliation,
        )
