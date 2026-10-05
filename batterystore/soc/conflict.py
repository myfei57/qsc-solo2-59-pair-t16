"""SOC 两个来源（库仑累计 / BMS 校准）的冲突记录。

累计和校准是 SOC 的两个独立来源：累计随充放电连续推进，校准在某个时刻
整体改写基准。判断"谁覆盖谁"只看事件发生时刻——更晚发生的那次为准，
更早的那次若在更晚事件之后才到达（乱序），即判定为过期事件：不参与
估算，原样留痕，供审计核对"这次读数为什么没被采纳"。
"""

from __future__ import annotations

from threading import RLock


class SourceConflictLog:
    """记录两个来源之间"晚到事件被丢弃"的冲突。"""

    def __init__(self, limit: int = 256) -> None:
        self._limit = max(1, limit)
        self._entries: list[dict] = []
        self._lock = RLock()

    def note_rejected(self, *, kind: str, ts: float, cutoff_ts: float, winner: str, detail: dict) -> dict:
        """登记一条被更晚事件压制的过期事件，返回该记录。"""

        with self._lock:
            entry = {
                "kind": kind,
                "rejected_ts": round(float(ts), 3),
                "winner": winner,
                "winner_ts": round(float(cutoff_ts), 3),
                "lag_s": round(max(0.0, float(cutoff_ts) - float(ts)), 3),
                "detail": {key: (round(value, 6) if isinstance(value, float) else value) for key, value in detail.items()},
            }
            self._entries.append(entry)
            if len(self._entries) > self._limit:
                del self._entries[: len(self._entries) - self._limit]
            return dict(entry)

    def latest(self) -> dict | None:
        """返回最近一条冲突记录。"""

        with self._lock:
            return dict(self._entries[-1]) if self._entries else None

    def entries(self, limit: int = 20) -> list[dict]:
        """返回最近的冲突记录（最新的在前）。"""

        with self._lock:
            return [dict(item) for item in self._entries[-limit:][::-1]]

    def count(self) -> int:
        """返回保留窗口内的冲突条数。"""

        with self._lock:
            return len(self._entries)
