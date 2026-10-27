"""Mock 行情推送器：完全离线，用随机游走持续"喂" K 线。

用途：教室断网、校园网封币安、云服务器出口被限的时候，
后端依然能像真实行情一样每 0.4 秒推进一次，演示动线完全不变。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass

from backend.config import Settings
from backend.store import CandleStore, Key
from core.synthetic import VOLATILITY_BY_INTERVAL, default_base_price
from core.timeframe import interval_ms


@dataclass
class MockStats:
    connected: bool = True
    messages: int = 0
    last_message_ts: float | None = None

    def as_dict(self) -> dict:
        now = time.time()
        return {
            **asdict(self),
            "last_message_age_s": round(now - self.last_message_ts, 2) if self.last_message_ts else None,
        }


class MockKlineFeed:
    def __init__(self, settings: Settings, store: CandleStore) -> None:
        self.settings = settings
        self.store = store
        self.stats = MockStats()
        self._stop = asyncio.Event()
        self._rng = random.Random(20241027)

    async def run(self) -> None:
        keys = self.settings.stream_keys
        anchors: dict[Key, float] = {
            key: (self.store.latest_price(key) or default_base_price(key[0])) for key in keys
        }
        logger_note = "Mock 行情已启动（不联网，随机游走）"
        print(f"[mock] {logger_note}：" + ", ".join(f"{s}@{i}" for s, i in keys), flush=True)

        while not self._stop.is_set():
            now_ms = int(time.time() * 1000)
            for key in keys:
                self._tick(key, now_ms, anchors[key])
            self.stats.messages += len(keys)
            self.stats.last_message_ts = time.time()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.mock_tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    def _tick(self, key: Key, now_ms: int, anchor: float) -> None:
        symbol, interval = key
        step = interval_ms(interval)
        bucket = now_ms - now_ms % step
        last = self.store.latest(key)
        if last is None:
            return

        if bucket > int(last["t"]):
            if not last.get("closed", True):
                closed = dict(last)
                closed["closed"] = True
                self.store.upsert(key, closed)
            base = float(last["c"])
            current = {
                "t": bucket,
                "o": base,
                "h": base,
                "l": base,
                "c": base,
                "v": 0.0,
                "closed": False,
            }
        elif bucket == int(last["t"]):
            current = dict(last)
        else:
            return

        vol = VOLATILITY_BY_INTERVAL.get(interval, 0.002) * 0.15
        drift = (anchor - current["c"]) / anchor * 0.01
        current["c"] = max(current["c"] * (1.0 + self._rng.gauss(drift, vol)), 1e-8)
        current["h"] = max(current["h"], current["c"])
        current["l"] = min(current["l"], current["c"])
        current["v"] = current["v"] + abs(self._rng.gauss(1.0, 0.5)) * max(anchor, 1.0) ** 0.5
        self.store.upsert(key, current)

    def health(self) -> dict:
        return {"source": "mock", **self.stats.as_dict()}
