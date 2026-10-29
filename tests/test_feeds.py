"""后端行情兜底单测：WebSocket 断线时必须能用 REST 补上数据。

背景：曾经只有启动时预热一次 REST，WS 握手失败后数据会**永久冻结**，
而 `/api/health` 的 HTTP 仍是 200、前端仍显示"后端实时" —— 用户看到的是几小时前的行情。
"""

from __future__ import annotations

import asyncio

from backend import feeds as feeds_module
from backend.config import get_settings
from backend.feeds import FeedManager
from backend.store import CandleStore


def make_manager(maxlen: int = 100) -> FeedManager:
    return FeedManager(get_settings(), CandleStore(maxlen=maxlen))


def test_ws_stale_when_no_feed() -> None:
    manager = make_manager()
    assert manager._ws_is_stale() is True


def test_refresh_via_rest_fills_store(monkeypatch) -> None:
    manager = make_manager()
    calls: dict[tuple[str, str], int] = {}

    def fake_fetch(bases, symbol, interval, limit):  # noqa: ANN001
        calls[(symbol, interval)] = limit
        return (
            [
                {"t": 1_000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0},
                {"t": 61_000, "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0, "v": 12.0},
            ],
            bases[0],
        )

    monkeypatch.setattr(feeds_module, "fetch_klines", fake_fetch)
    refreshed = asyncio.run(manager.refresh_via_rest(limit=50))

    assert refreshed == len(manager.settings.stream_keys)
    assert len(calls) == refreshed
    assert all(limit == 50 for limit in calls.values())
    counts = manager.store.counts()
    assert counts and all(value == 2 for value in counts.values())
    assert manager.last_rest_refresh is not None


def test_refresh_via_rest_keeps_history_and_appends(monkeypatch) -> None:
    """REST 补数据不能把更早的历史冲掉：只合并新 K 线。"""
    manager = make_manager(maxlen=500)
    key = manager.settings.stream_keys[0]
    manager.store.bulk_load(key, [{"t": 1_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])

    def fake_fetch(bases, symbol, interval, limit):  # noqa: ANN001
        if (symbol, interval) != key:
            return [], None
        return ([{"t": 61_000, "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}], bases[0])

    monkeypatch.setattr(feeds_module, "fetch_klines", fake_fetch)
    asyncio.run(manager.refresh_via_rest(limit=10))

    times = [candle["t"] for candle in manager.store.candles(key)]
    assert times == [1_000, 61_000]
