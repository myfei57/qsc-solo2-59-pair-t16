"""SOC 状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SocSnapshot:
    """SOC 某一时刻的状态。"""

    current: dict
    calibration: dict
    coulomb: dict
    margin_pct: float
    samples: int
    reconciliation: dict
    conflicts: list

    def as_dict(self) -> dict:
        return {
            "current": dict(self.current),
            "calibration": dict(self.calibration),
            "coulomb": dict(self.coulomb),
            "margin_pct": round(self.margin_pct, 3),
            "samples": self.samples,
            "reconciliation": dict(self.reconciliation),
            "conflicts": [dict(item) for item in self.conflicts],
        }
