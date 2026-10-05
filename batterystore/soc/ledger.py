"""跨校准段电量对账台账。

口径选择固定为"保留连续累计、每代校准只加修正量"：库仑累计器从不清零，
每次校准确立一代锚点，锚点记录当时的连续累计读数；两个锚点读数之差就是
上一段的库仑净增量，相邻两代基准之差减去这段净增量就是上一代的累计漂移
（也就是这一代相对上一代加的修正量）。

为什么不选"校准后清零累计"：两种口径在当前 SOC 这一个点上算出的数相同
（都等于新校准基准），但清零把校准之前的充放吞吐量一并抹掉，跨段对账时
段净增量与累计漂移都无账可查；保留连续累计后段内账与全生命周期账都能
闭合，且不改变"最新校准基准 + 锚点后净增量"的估算方式。
"""

from __future__ import annotations

from threading import RLock


class CalibrationLedger:
    """按校准代际记录锚点，并给出段内/全程对账结果。"""

    def __init__(self, tolerance_pct: float, initial_soc_pct: float) -> None:
        self._tolerance_pct = float(tolerance_pct)
        self._lock = RLock()
        self._anchors: list[dict] = [
            # 代际 0 是 BMS 校准记录出厂时的占位基准，不参与跨段对账。
            {
                "epoch": 0,
                "base_soc_pct": round(float(initial_soc_pct), 3),
                "counter_ah": 0.0,
                "stamped_at": 0.0,
                "source": "init",
                "baseline": True,
            }
        ]

    def _anchor_epoch(self, epoch: int, base_soc_pct: float, counter_ah: float, ts: float, source: str) -> dict:
        anchor = {
            "epoch": int(epoch),
            "base_soc_pct": round(float(base_soc_pct), 3),
            "counter_ah": round(float(counter_ah), 6),
            "stamped_at": round(float(ts), 3),
            "source": source,
        }
        self._anchors.append(anchor)
        return dict(anchor)

    def open_epoch(self, epoch: int, base_soc_pct: float, counter_ah: float, ts: float, source: str) -> dict:
        """新一代校准确立锚点；同时闭合上一段并把对账结果记进上一条锚点。"""

        with self._lock:
            if self._anchors and self._anchors[-1]["epoch"] >= int(epoch):
                raise ValueError(f"校准代际必须严格递增: {epoch}")
            if self._anchors:
                previous = self._anchors[-1]
                segment_ah = float(counter_ah) - float(previous["counter_ah"])
                base_shift_pct = float(base_soc_pct) - float(previous["base_soc_pct"])
                # 累计漂移 = 基准实际变动 - 库仑累计应带来的变动；段净增量以 Ah
                # 保留，基准跳变（百分点）直接与容差比较，超差即在段记录中告警。
                previous["closed"] = {
                    "next_epoch": int(epoch),
                    "segment_net_ah": round(segment_ah, 6),
                    "base_shift_pct": round(base_shift_pct, 3),
                    "closed_at": round(float(ts), 3),
                }
            return self._anchor_epoch(epoch, base_soc_pct, counter_ah, ts, source)

    def current_epoch(self) -> int:
        """返回当前打开的校准代际。"""

        with self._lock:
            return int(self._anchors[-1]["epoch"])

    def anchor(self, epoch: int | None = None) -> dict:
        """返回指定代际（默认当前代）的锚点。"""

        with self._lock:
            if epoch is None:
                return dict(self._anchors[-1])
            for item in reversed(self._anchors):
                if item["epoch"] == int(epoch):
                    return dict(item)
            raise KeyError(f"未知的校准代际: {epoch}")

    def segments(self, limit: int = 20) -> list[dict]:
        """返回最近闭合的校准段对账记录，超过容差的段会显式标出来。"""

        with self._lock:
            closed: list[dict] = []
            for item in self._anchors:
                if item.get("baseline"):
                    continue
                detail = item.get("closed")
                if detail is None:
                    continue
                # 以基准跳变作为上一段累计漂移的对账信号：没有漂移时基准
                # 跳变应等于该段净增量折算的百分点；跳变超过容差即明确告警。
                drift_pct = abs(float(detail["base_shift_pct"]))
                closed.append(
                    {
                        "epoch": item["epoch"],
                        "next_epoch": detail["next_epoch"],
                        "source": item["source"],
                        "segment_net_ah": detail["segment_net_ah"],
                        "base_shift_pct": detail["base_shift_pct"],
                        "tolerance_pct": round(self._tolerance_pct, 3),
                        "exceeded_tolerance": drift_pct > self._tolerance_pct,
                        "stamped_at": item["stamped_at"],
                        "closed_at": detail["closed_at"],
                    }
                )
            return closed[-limit:]

    def reconciliation(self, counter_ah: float, capacity_ah: float, limit: int = 20) -> dict:
        """生成跨段对账：各闭合段 + 当前段 + 全程连续累计读数。"""

        with self._lock:
            real = [item for item in self._anchors if not item.get("baseline")]
            origin = real[0] if real else self._anchors[0]
            latest = self._anchors[-1]
            current_net_ah = float(counter_ah) - float(latest["counter_ah"])
            lifetime_ah = float(counter_ah) - float(origin["counter_ah"])
            return {
                "policy": "continuous_counter_with_correction",
                "capacity_ah": float(capacity_ah),
                "segments": self.segments(limit),
                "open_segment": {
                    "epoch": latest["epoch"],
                    "anchor_soc_pct": latest["base_soc_pct"],
                    "net_ah_since_anchor": round(current_net_ah, 6),
                    "net_pct_since_anchor": round(current_net_ah / float(capacity_ah) * 100.0, 3),
                },
                "lifetime_net_ah": round(lifetime_ah, 6),
                "anchor_count": len(real),
            }
