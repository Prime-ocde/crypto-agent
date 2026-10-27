"""内存行情缓存：每个 (symbol, interval) 一个环形缓冲，线程安全。

为什么不上 Redis / SQLite：
- 演示场景单进程单用户，数据是"热数据"，落盘反而增加故障面；
- 但**缓存替换策略**必须存在，否则长时间跑内存会一路涨 —— 用 deque(maxlen) 就是最简单正确的答案。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable

Candle = dict
Key = tuple[str, str]


def make_key(symbol: str, interval: str) -> Key:
    """注意：**周期必须保留大小写**，`1m`（分钟）与 `1M`（月）是两个不同周期。"""
    return symbol.upper(), interval


class CandleStore:
    def __init__(self, maxlen: int = 1000) -> None:
        if maxlen < 10:
            raise ValueError("缓存长度至少为 10 根 K 线")
        self._maxlen = int(maxlen)
        self._data: dict[Key, deque[Candle]] = {}
        self._last_update: dict[Key, float] = {}
        self._lock = threading.RLock()

    # ---------------- 写入 ----------------
    def upsert(self, key: Key, candle: Candle) -> None:
        """插入/更新一根 K 线。

        Binance 会把同一根"未收盘"K 线反复推送，因此同一个 t 必须**原地替换**，
        否则图上会出现一堆时间戳重复的蜡烛。
        """
        if not candle:
            return
        ts = int(candle["t"])
        with self._lock:
            series = self._data.setdefault(key, deque(maxlen=self._maxlen))
            if series and ts == series[-1]["t"]:
                series[-1] = candle
            elif not series or ts > series[-1]["t"]:
                series.append(candle)
            else:
                return  # 乱序数据包直接丢弃
            self._last_update[key] = time.time()

    def bulk_load(self, key: Key, candles: Iterable[Candle]) -> int:
        """启动预热：一次性灌入历史 K 线（按时间升序，超过 maxlen 自动丢最旧的）。"""
        items = sorted((c for c in candles if c), key=lambda c: int(c["t"]))
        if not items:
            return 0
        with self._lock:
            series: deque[Candle] = deque(items[-self._maxlen :], maxlen=self._maxlen)
            self._data[key] = series
            self._last_update[key] = time.time()
        return len(items)

    # ---------------- 读取 ----------------
    def candles(self, key: Key, limit: int | None = None) -> list[Candle]:
        with self._lock:
            series = self._data.get(key)
            if not series:
                return []
            items = list(series)
        if limit is not None and limit > 0:
            items = items[-limit:]
        return items

    def latest(self, key: Key) -> Candle | None:
        with self._lock:
            series = self._data.get(key)
            return series[-1] if series else None

    def latest_price(self, key: Key) -> float | None:
        candle = self.latest(key)
        return float(candle["c"]) if candle else None

    def last_update(self, key: Key) -> float | None:
        with self._lock:
            return self._last_update.get(key)

    def has(self, key: Key) -> bool:
        with self._lock:
            return bool(self._data.get(key))

    def keys(self) -> list[Key]:
        with self._lock:
            return list(self._data)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {f"{symbol}|{interval}": len(series) for (symbol, interval), series in self._data.items()}

    def total(self) -> int:
        with self._lock:
            return sum(len(series) for series in self._data.values())
