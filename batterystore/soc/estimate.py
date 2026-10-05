"""SOC 估算。

估算口径固定为"当前代校准基准 + 该代锚点之后的库仑净增量"，校准代际原样
带出。按校准之前的偏移继续折算，就会出现实际剩 30% 却显示 45% 的偏差；
把代际写进结果，调用方能直接核对这次估算用的是哪一代校准。

结果同时给出调度最近一次采用的电量（reported_pct）与当前估算的偏差：
偏差绝对值超过容差时 ``deviation_exceeded`` 置位，调用方必须据此明确
告警，不能再按旧电量安排放电。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SocLimits


@dataclass(frozen=True)
class SocEstimate:
    """一次 SOC 估算结果。"""

    soc_pct: float
    calibration_epoch: int
    base_soc_pct: float
    delta_pct: float
    stale: bool
    drift_pct: float
    reported_pct: float
    tolerance_pct: float
    deviation_exceeded: bool

    def as_dict(self) -> dict:
        return {
            "soc_pct": round(self.soc_pct, 3),
            "calibration_epoch": self.calibration_epoch,
            "base_soc_pct": round(self.base_soc_pct, 3),
            "delta_pct": round(self.delta_pct, 3),
            "stale": self.stale,
            "drift_pct": round(self.drift_pct, 3),
            "reported_pct": round(self.reported_pct, 3),
            "tolerance_pct": round(self.tolerance_pct, 3),
            "deviation_exceeded": self.deviation_exceeded,
        }


def estimate_soc(
    calibration: dict,
    net_ah: float,
    limits: SocLimits,
    reported_pct: float,
) -> SocEstimate:
    """按最近一代校准与库仑净增量估算 SOC。"""

    base = float(calibration.get("soc_pct", limits.initial_soc_pct))
    delta = limits.amp_hours_to_percent(float(net_ah))
    raw = base + delta
    bounded = max(0.0, min(100.0, raw))
    reported = float(reported_pct)
    deviation = bounded - reported
    tolerance = float(limits.drift_tolerance_pct)
    return SocEstimate(
        soc_pct=bounded,
        calibration_epoch=int(calibration.get("epoch", 0)),
        base_soc_pct=base,
        delta_pct=delta,
        stale=bool(calibration.get("expired", False)),
        drift_pct=deviation,
        reported_pct=reported,
        tolerance_pct=tolerance,
        deviation_exceeded=abs(deviation) > tolerance,
    )
