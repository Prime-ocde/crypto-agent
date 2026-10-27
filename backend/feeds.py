"""行情数据源编排：启动预热（REST → CSV 缓存 → 合成） + 数据源生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.binance_rest import fetch_klines, load_cache, save_cache
from backend.binance_ws import BinanceKlineFeed
from backend.config import APP_VERSION, Settings
from backend.mock_feed import MockKlineFeed
from backend.store import CandleStore, Key
from core.synthetic import synthesize_candles

logger = logging.getLogger("grid.backend.feeds")

SOURCE_LABELS = {
    "rest": "Binance REST 预热",
    "cache": "本地 CSV 缓存",
    "synthetic": "本地合成行情",
    "binance_ws": "Binance WebSocket 实时",
    "mock": "Mock 模拟行情",
}


class FeedManager:
    def __init__(self, settings: Settings, store: CandleStore) -> None:
        self.settings = settings
        self.store = store
        self.started_at = time.time()
        self._feed: BinanceKlineFeed | MockKlineFeed | None = None
        self._task: asyncio.Task | None = None
        self._source: dict[Key, str] = {}
        # REST 域名按"最近成功的排最前"排序：主域名被墙时只吃一次超时，不拖慢整轮预热
        self._rest_bases: list[str] = list(dict.fromkeys([settings.rest_url, settings.rest_fallback]))

    # ---------------- 启动 / 停止 ----------------
    async def start(self) -> None:
        await self.bootstrap()
        if self.settings.is_mock:
            self._feed = MockKlineFeed(self.settings, self.store)
        else:
            self._feed = BinanceKlineFeed(self.settings, self.store)
        self._task = asyncio.create_task(self._feed.run(), name=f"{self.settings.run_mode}-kline-feed")

        live_source = "mock" if self.settings.is_mock else "binance_ws"
        for key in self.settings.stream_keys:
            self._source[key] = live_source

    async def bootstrap(self) -> None:
        """先把图表填满，再谈实时：REST → 本地缓存 → 合成，逐级降级。"""
        for key in self.settings.stream_keys:
            symbol, interval = key
            candles: list[dict] = []
            source = "synthetic"

            if not self.settings.is_mock:
                candles, used_base = await asyncio.to_thread(
                    fetch_klines,
                    tuple(self._rest_bases),
                    symbol,
                    interval,
                    self.settings.kline_limit,
                )
                if used_base:
                    self._rest_bases.remove(used_base)
                    self._rest_bases.insert(0, used_base)
                if candles:
                    source = "rest"
                    try:
                        await asyncio.to_thread(save_cache, symbol, interval, candles)
                    except OSError as exc:
                        logger.warning("写入本地缓存失败 %s %s: %s", symbol, interval, exc)
                else:
                    candles = await asyncio.to_thread(load_cache, symbol, interval)
                    if candles:
                        source = "cache"

            if not candles:
                candles = synthesize_candles(
                    symbol,
                    interval,
                    limit=min(self.settings.kline_limit, 500),
                )
                source = "synthetic"

            self._source[key] = source
            loaded = self.store.bulk_load(key, candles)
            logger.info(
                "预热 %s %s：%s 根（%s → %s）", symbol, interval, loaded, source, SOURCE_LABELS[source]
            )

    async def stop(self) -> None:
        if self._feed is not None:
            await self._feed.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关停阶段不再抛错
                pass
            self._task = None
        self._feed = None

    # ---------------- 查询 ----------------
    @property
    def feed(self) -> Any:
        return self._feed

    def source_of(self, key: Key) -> str:
        return self._source.get(key, "unknown")

    def health(self) -> dict:
        settings = self.settings
        now = time.time()
        feed_health = self._feed.health() if self._feed is not None else {"connected": False}

        ages: dict[str, float | None] = {}
        for key in settings.stream_keys:
            stamp = self.store.last_update(key)
            ages[f"{key[0]}|{key[1]}"] = round(now - stamp, 2) if stamp else None

        status = "ok"
        if not settings.is_mock:
            last_message = feed_health.get("last_message_ts")
            idle = (now - last_message) if last_message else None
            if not feed_health.get("connected") or idle is None or idle > settings.idle_timeout:
                status = "degraded"

        return {
            "status": status,
            "version": APP_VERSION,
            "mode": settings.run_mode,
            "uptime_s": round(now - self.started_at, 1),
            "feed": feed_health,
            "sources": {f"{s}|{i}": SOURCE_LABELS.get(self._source.get((s, i), ""), "未知") for s, i in settings.stream_keys},
            "candle_counts": self.store.counts(),
            "data_age_s": ages,
            "ws_retry": {
                "base_s": settings.backoff_base,
                "max_s": settings.backoff_max,
                "jitter_ratio": settings.jitter_ratio,
                "idle_timeout_s": settings.idle_timeout,
            },
        }
