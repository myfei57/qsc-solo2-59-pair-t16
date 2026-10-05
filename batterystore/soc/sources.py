"""SOC 两个数据来源的先后账。

SOC 同时被两类事件改写：充放电过程持续不断的**库仑累计**，以及偶发的
**BMS 校准**。它们是两个独立来源，到达顺序不保证——现场完全可能先按旧基准
累计了一段、之后才收到一版时刻更早但登记更晚的校准，或校准已落、一段迟到
的累计样本才上报。

规则固定为**以更晚发生的那次为准（last-writer-wins）**：任何写入（无论
来源）只要时刻早于最近一次已生效写入，就判负丢弃、状态不回退；两个来源
不同的判负还要留下冲突记录（哪两个来源、各自时刻、谁赢了、判负那次的
内容），同源的迟到样本只丢弃不留冲突（那是遥测乱序，不是两个基准在打架）。
只"以后到的为准"而不留痕，跨班次就无法解释 SOC 为什么跳变。
"""

from __future__ import annotations

CALIBRATION_SOURCE = "calibration"
COULOMB_SOURCE = "coulomb"


class SourceLedger:
    """记录累计/校准两类来源的写入先后与冲突裁决。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._conflicts: list[dict] = []

    def verdict(self, source: str, ts: float) -> dict:
        """只做先后裁决不落账：本次写入会不会被上一条压住。

        ``stale``：时刻早于最近一次生效写入，一律判负；
        ``conflict``：判负且双方来源不同，需要留跨来源冲突记录。
        """

        previous = self._events[-1] if self._events else None
        stale = previous is not None and float(ts) < previous["ts"]
        return {
            "stale": stale,
            "winner": previous["source"] if stale else source,
            "conflict": bool(stale and previous["source"] != source),
            "previous": dict(previous) if previous else None,
        }

    def commit(self, source: str, ts: float, detail: dict | None = None) -> dict:
        """把一次生效写入落账，返回该条记录。"""

        entry = {
            "seq": len(self._events) + 1,
            "source": source,
            "ts": round(float(ts), 3),
            "detail": dict(detail or {}),
        }
        self._events.append(entry)
        return dict(entry)

    def record_rejection(self, source: str, ts: float, detail: dict | None = None) -> dict:
        """把一次已判定迟到的跨来源写入记为冲突（调用方已先做过 verdict 裁决）。"""

        previous = self._events[-1] if self._events else None
        held_source = previous["source"] if previous else ""
        held_ts = previous["ts"] if previous else 0.0
        record = {
            "seq": len(self._events) + len(self._conflicts) + 1,
            "winner": held_source,
            "rejected_source": source,
            "rejected_ts": round(float(ts), 3),
            "rejected_detail": dict(detail or {}),
            "held_source": held_source,
            "held_ts": held_ts,
            "lag_s": round(held_ts - float(ts), 3),
        }
        self._conflicts.append(record)
        return dict(record)

    def conflicts(self, limit: int = 20) -> list[dict]:
        """返回最近若干次来源冲突裁决记录。"""

        return [dict(item) for item in self._conflicts[-limit:]]

    def events(self, limit: int = 60) -> list[dict]:
        """返回最近若干条生效的来源写入。"""

        return [dict(item) for item in self._events[-limit:]]

    def last_source(self) -> str | None:
        """返回最近一次生效写入的来源。"""

        return self._events[-1]["source"] if self._events else None

    def last_ts(self) -> float | None:
        """返回最近一次生效写入的时刻。"""

        return self._events[-1]["ts"] if self._events else None
