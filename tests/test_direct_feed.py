"""直连行情单测：解析、派生周期聚合、失败降级。"""

from __future__ import annotations

import pytest

from core.direct_feed import fetch_klines, parse_klines


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_parse_klines_converts_types() -> None:
    rows = [[1700000000000, "100.0", "101.0", "99.0", "100.5", "12.5"]]
    candles = parse_klines(rows)
    assert candles == [
        {"t": 1700000000000, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 12.5, "closed": True}
    ]


def test_parse_klines_skips_bad_rows() -> None:
    assert parse_klines([[1, "bad"]]) == []
    assert parse_klines(None) == []


def test_fetch_klines_returns_parsed_rows(monkeypatch) -> None:
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params))
        return FakeResponse([[1700000000000, "1", "2", "0.5", "1.5", "9"]])

    monkeypatch.setattr("core.direct_feed.requests.get", fake_get)
    candles = fetch_klines("BTCUSDT", "1m", limit=10)
    assert len(candles) == 1
    assert candles[0]["c"] == 1.5
    assert calls[0][1]["symbol"] == "BTCUSDT"
    assert calls[0][1]["interval"] == "1m"


def test_fetch_klines_derives_yearly(monkeypatch) -> None:
    def fake_get(url, params=None, timeout=None, headers=None):
        assert params["interval"] == "1M"
        # 24 个月 → 2 根年 K
        rows = []
        for index in range(24):
            year = 2024 + index // 12
            month = index % 12 + 1
            ts = 1704067200000 + index * 31 * 86400000  # 近似递增时间戳
            rows.append([ts, "100", "110", "90", "105", "1"])
        return FakeResponse(rows)

    monkeypatch.setattr("core.direct_feed.requests.get", fake_get)
    candles = fetch_klines("BTCUSDT", "1y", limit=5)
    assert len(candles) == 2
    assert all(candle["c"] == 105.0 for candle in candles)


def test_fetch_klines_returns_empty_on_failure(monkeypatch) -> None:
    def fake_get(url, params=None, timeout=None, headers=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("core.direct_feed.requests.get", fake_get)
    assert fetch_klines("BTCUSDT", "1m") == []
