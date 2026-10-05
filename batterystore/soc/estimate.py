"""SOC 估算。

估算口径固定为"最近一代校准基准 + 该代校准之后的库仑净增量"，校准代际原样带出。
按校准之前的偏移继续折算，就会出现实际剩 30% 却显示 45% 的偏差；把代际写进结果，
调用方能直接核对这次估算用的是哪一代校准。

两类偏差都显式带出、不由调用方自己猜：

* ``drift_pct``：估算值减去最近一次权威基准（校准/外部上报）的带符号差，
  累计推算相对基准漂出容差时置 ``drift_exceeded``——正值账面偏低、电量更足，
  负值账面虚高、存在过度放电风险；
* ``correction_pct``：新一代校准值相对"校准前累计推算值"的带符号跳变量，
  绝对值超过容差时置 ``correction_exceeded``——这正是"实际剩 30% 却显示
  45%"在校准一刻被抓到的那个偏差。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

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
    drift_tolerance_pct: float
    drift_exceeded: bool
    correction_pct: float = 0.0
    correction_exceeded: bool = False

    def as_dict(self) -> dict:
        return {
            "soc_pct": round(self.soc_pct, 3),
            "calibration_epoch": self.calibration_epoch,
            "base_soc_pct": round(self.base_soc_pct, 3),
            "delta_pct": round(self.delta_pct, 3),
            "stale": self.stale,
            "drift_pct": round(self.drift_pct, 3),
            "drift_tolerance_pct": round(self.drift_tolerance_pct, 3),
            "drift_exceeded": self.drift_exceeded,
            "correction_pct": round(self.correction_pct, 3),
            "correction_exceeded": self.correction_exceeded,
        }

    def with_correction(self, previous_soc_pct: float) -> "SocEstimate":
        """按"新校准值 − 校准前估算值"填装校准跳变判定。"""

        correction = self.soc_pct - float(previous_soc_pct)
        return replace(
            self,
            correction_pct=correction,
            correction_exceeded=abs(correction) > self.drift_tolerance_pct,
        )


def estimate_soc(
    calibration: dict,
    net_ah: float,
    limits: SocLimits,
    anchor_pct: float,
) -> SocEstimate:
    """按最近一代校准与该代之后的库仑净增量估算 SOC。"""

    base = float(calibration.get("soc_pct", limits.initial_soc_pct))
    delta = limits.amp_hours_to_percent(float(net_ah))
    raw = base + delta
    bounded = max(0.0, min(100.0, raw))
    tolerance = float(limits.drift_tolerance_pct)
    drift = bounded - float(anchor_pct)
    return SocEstimate(
        soc_pct=bounded,
        calibration_epoch=int(calibration.get("epoch", 0)),
        base_soc_pct=base,
        delta_pct=delta,
        stale=bool(calibration.get("expired", False)),
        drift_pct=drift,
        drift_tolerance_pct=tolerance,
        drift_exceeded=abs(drift) > tolerance,
    )
