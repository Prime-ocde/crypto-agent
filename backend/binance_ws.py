"""Binance 公共 WebSocket 行情客户端。

演示时最容易被问的三点，代码里都有对应实现：

1. **指数退避重连**：`delay = min(max, base × 2^failures) × (1 ± jitter)`，
   避免断网恢复瞬间所有客户端同时重连把对端打爆（抖动让重连时间错开）。
2. **假死检测**：币安有时会保持 TCP 连接但不再推送数据。只依赖 `on_close` 会永远卡住，
   所以每次 `recv()` 都带 `idle_timeout`，超时就主动断开重建。
3. **只读行情**：只消费 `@kline_` 公共流，不接触任何下单/账户接口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field

import websockets

from backend.config import Settings
from backend.store import CandleStore, Key

logger = logging.getLogger("grid.backend.ws")


@dataclass
class FeedStats:
    connected: bool = False
    reconnects: int = 0
    messages: int = 0
    last_message_ts: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_retry_delay: float = 0.0
    connected_since: float | None = None
    endpoint: str | None = None

    def as_dict(self) -> dict:
        now = time.time()
        return {
            **asdict(self),
            "last_message_age_s": round(now - self.last_message_ts, 2) if self.last_message_ts else None,
            "connected_for_s": round(now - self.connected_since, 1) if self.connected_since else None,
        }


class BinanceKlineFeed:
    def __init__(self, settings: Settings, store: CandleStore) -> None:
        self.settings = settings
        self.store = store
        self.stats = FeedStats()
        self._stop = asyncio.Event()
        self._endpoint_index = 0

    # ---------------- URL ----------------
    @property
    def endpoints(self) -> list[str]:
        """主域名 + 备用域名。阶段二起主域名是 `data-stream.binance.vision`，
        不可达时按顺序切到 `stream.binance.com` 的备用地址。"""
        bases = (self.settings.ws_url, *self.settings.ws_fallbacks)
        streams = "/".join(
            f"{symbol.lower()}@kline_{interval}" for symbol, interval in self.settings.stream_keys
        )
        urls: list[str] = []
        for base in bases:
            root = base.rstrip("/")
            if root.endswith("/ws"):
                root = root[: -len("/ws")]
            url = f"{root}/stream?streams={streams}"
            if url not in urls:
                urls.append(url)
        return urls

    @property
    def stream_url(self) -> str:
        endpoints = self.endpoints
        return endpoints[self._endpoint_index % len(endpoints)]

    # ---------------- 生命周期 ----------------
    async def run(self) -> None:
        while not self._stop.is_set():
            url = self.stream_url
            self.stats.endpoint = url.split("?", 1)[0]
            try:
                logger.info(
                    "连接 Binance WebSocket（%s 个组合流，端点 %s）",
                    len(self.settings.stream_keys),
                    self.stats.endpoint,
                )
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=12,
                    max_queue=2048,
                ) as socket:
                    self.stats.connected = True
                    self.stats.connected_since = time.time()
                    self.stats.consecutive_failures = 0
                    self.stats.next_retry_delay = 0.0
                    logger.info("WebSocket 已连接：%s", self.stats.endpoint)
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=self.settings.idle_timeout)
                        except asyncio.TimeoutError:
                            raise RuntimeError(
                                f"超过 {self.settings.idle_timeout:.0f}s 未收到行情，判定连接假死，主动重建"
                            ) from None
                        self.stats.messages += 1
                        self.stats.last_message_ts = time.time()
                        self._handle_payload(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络异常统一进入退避重连
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.stats.consecutive_failures += 1
                self.stats.reconnects += 1
                # 换下一个端点再试：主域名被墙时这一步就是救命稻草
                self._endpoint_index += 1
                logger.warning(
                    "WebSocket 异常（连续失败 %s 次，下次端点 %s）：%s",
                    self.stats.consecutive_failures,
                    self.endpoints[self._endpoint_index % len(self.endpoints)].split("?", 1)[0],
                    exc,
                )
            finally:
                self.stats.connected = False
                self.stats.connected_since = None

            if self._stop.is_set():
                break

            delay = self.backoff_delay(self.stats.consecutive_failures)
            self.stats.next_retry_delay = round(delay, 2)
            logger.info("指数退避：%.1fs 后重连", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    # ---------------- 内部 ----------------
    def backoff_delay(self, failures: int) -> float:
        exponent = min(max(failures - 1, 0), 10)
        base = self.settings.backoff_base * (2**exponent)
        delay = min(self.settings.backoff_max, base)
        jitter = delay * self.settings.jitter_ratio
        return max(0.2, delay + random.uniform(-jitter, jitter))

    def _handle_payload(self, raw: str | bytes) -> None:
        text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return

        data = payload.get("data", payload)
        kline = data.get("k") if isinstance(data, dict) else None
        if not isinstance(kline, dict):
            return  # 订阅确认 / 心跳等非 K 线消息

        symbol = str(data.get("s") or kline.get("s") or "").upper()
        interval = str(kline.get("i") or "")
        if not symbol or not interval:
            return

        candle = {
            "t": int(kline["t"]),
            "o": float(kline["o"]),
            "h": float(kline["h"]),
            "l": float(kline["l"]),
            "c": float(kline["c"]),
            "v": float(kline.get("v") or 0.0),
            "closed": bool(kline.get("x", False)),
        }
        self.store.upsert((symbol, interval), candle)

    def health(self) -> dict:
        return {"source": "binance_ws", **self.stats.as_dict()}
