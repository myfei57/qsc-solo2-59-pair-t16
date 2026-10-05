"""校准分段的库仑账。

跨校准段的电量对账有两种都说得通的口径：

* **口径一（清零重起）**：校准一刻把库仑累计清零，新校准值就是新段的零点，
  之后每段的净安时都只在本段内累计。
* **口径二（保留累计加修正量）**：累计器永不清零，只在基准上叠加一个修正量。

本实现固定采用**口径一**，理由：

1. 校准本身就是"以现场实测值替换推算值"的动作，旧段累计里携带的误差（效率
   偏差、漏电流、采样零点漂移）在口径二里会被原样带进新基准，校准只能盖一个
   修正量、却无法把旧误差从账上剔除；
2. 库仑计带有充电效率折算，跨校准时刻把"按旧效率折算的安时"与新基准直接
   相加，折算口径不一致；清零后新段从零点重新折算，段内账自洽；
3. 每段净增量闭合（段起点 SOC 由校准给出、段内变化由本段净安时给出、段尾
   由下一次校准闭合），跨段对账退化成"把各闭合段首尾相接"，审计可逐段核对，
   不需要解释一个修正量到底修正了哪一段的哪类误差。

口径一并不丢历史：每来一代校准就把当前段封存（段起点基准、起止时刻、清零前
净安时、与下代基准之间的修正量），另开新段。跨校准段对账时把各闭合段首尾
相接即可还原全程电量轨迹；旧代校准值与旧段累计仍可查。
"""

from __future__ import annotations

from .coulomb import CoulombCounter


class SegmentLedger:
    """按校准代际分段的库仑账：开段累计、封段留存。"""

    def __init__(self, efficiency: float) -> None:
        self._efficiency = float(efficiency)
        self._open_counter = CoulombCounter(self._efficiency)
        self._open_epoch: int | None = None
        self._open_base_pct: float = 0.0
        self._open_started_at: float = 0.0
        self._open_source: str = ""
        self._closed: list[dict] = []

    def open(self, epoch: int, base_soc_pct: float, stamped_at: float, source: str) -> None:
        """以一代校准为零点开一段（构造后或封存后调用）。"""

        self._open_counter = CoulombCounter(self._efficiency)
        self._open_epoch = int(epoch)
        self._open_base_pct = float(base_soc_pct)
        self._open_started_at = float(stamped_at)
        self._open_source = source

    def accumulate(self, current_a: float, dt: float) -> float:
        """在当前开段上累计一段安时，返回本段净安时增量。"""

        return self._open_counter.through(current_a, dt)

    def net_ah(self) -> float:
        """返回当前开段的净安时（充为正、放为负）。"""

        return self._open_counter.net_ah()

    def charged_ah(self) -> float:
        """返回当前开段累计充入安时。"""

        return self._open_counter.charged_ah()

    def discharged_ah(self) -> float:
        """返回当前开段累计放出安时。"""

        return self._open_counter.discharged_ah()

    def intervals(self) -> int:
        """返回当前开段的累计推进次数。"""

        return self._open_counter.intervals()

    def seal(self, capacity_ah: float, next_base_soc_pct: float) -> dict:
        """封存当前开段。

        ``correction_pct`` 是下代校准基准相对"按本段累计推算到段尾的 SOC"的
        带符号偏差，即这次校准把基准挪动了多少；只留痕，不参与新段累加。
        """

        projected = self._open_base_pct + self._open_counter.net_ah() / float(capacity_ah) * 100.0
        sealed = {
            "epoch": self._open_epoch,
            "base_soc_pct": round(self._open_base_pct, 3),
            "started_at": round(self._open_started_at, 3),
            "source": self._open_source,
            "charged_ah": round(self._open_counter.charged_ah(), 6),
            "discharged_ah": round(self._open_counter.discharged_ah(), 6),
            "net_ah": round(self._open_counter.net_ah(), 6),
            "intervals": self._open_counter.intervals(),
            "projected_soc_pct": round(projected, 3),
            "correction_pct": round(float(next_base_soc_pct) - projected, 3),
        }
        self._closed.append(sealed)
        return dict(sealed)

    def segments(self, limit: int = 20) -> list[dict]:
        """返回最近若干个已封存段（按校准先后排序）。"""

        return [dict(item) for item in self._closed[-limit:]]

    def reconcile(self, capacity_ah: float, current_soc_pct: float) -> list[dict]:
        """跨校准段对账：逐段给出基准、段内增量、校准修正量与段尾 SOC。

        口径一下段间闭合关系为：
        ``段尾 = 段起点基准 + 段内净安时折算 + 下代校准修正量``。
        已封存段的段尾就是下一段的起点基准；当前开段以最新估算值收尾。
        """

        rows: list[dict] = []
        for index, segment in enumerate(self._closed):
            delta_pct = segment["net_ah"] / float(capacity_ah) * 100.0
            projected = segment["base_soc_pct"] + delta_pct
            if index + 1 < len(self._closed):
                end_pct = self._closed[index + 1]["base_soc_pct"]
            else:
                end_pct = round(projected + segment["correction_pct"], 3)
            rows.append(
                {
                    "epoch": segment["epoch"],
                    "base_soc_pct": segment["base_soc_pct"],
                    "delta_pct": round(delta_pct, 3),
                    "correction_pct": segment["correction_pct"],
                    "projected_soc_pct": round(projected, 3),
                    "end_soc_pct": end_pct,
                    "net_ah": segment["net_ah"],
                }
            )
        if self._open_epoch is not None:
            delta_pct = self.net_ah() / float(capacity_ah) * 100.0
            rows.append(
                {
                    "epoch": self._open_epoch,
                    "base_soc_pct": round(self._open_base_pct, 3),
                    "delta_pct": round(delta_pct, 3),
                    "correction_pct": 0.0,
                    "projected_soc_pct": round(self._open_base_pct + delta_pct, 3),
                    "end_soc_pct": round(float(current_soc_pct), 3),
                    "net_ah": round(self.net_ah(), 6),
                }
            )
        return rows

    def reset_open(self) -> dict:
        """只清零当前开段（运维复位用），已封存段保留。"""

        return self._open_counter.reset()
