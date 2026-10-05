"""SOC 状态快照。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SocSnapshot:
    """SOC 某一时刻的状态。"""

    estimate: dict
    calibration: dict
    coulomb: dict
    margin_pct: float
    samples: int
    published: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    reconciliation: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "estimate": dict(self.estimate),
            "calibration": dict(self.calibration),
            "coulomb": dict(self.coulomb),
            "margin_pct": round(self.margin_pct, 3),
            "samples": self.samples,
            "published": dict(self.published),
            "segments": [dict(item) for item in self.segments],
            "conflicts": [dict(item) for item in self.conflicts],
            "reconciliation": [dict(item) for item in self.reconciliation],
        }
