"""API 契约测试：用 Mock 模式启动，完全不依赖外网。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ["RUN_MODE"] = "mock"
os.environ["KLINE_LIMIT"] = "300"
os.environ["MOCK_TICK_SECONDS"] = "0.2"

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_root_and_docs(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "crypto-grid-quant-backend"
    assert "模拟" in body["disclaimer"]  # 免责声明必须存在且说明是模拟盘


def test_health_reports_mock_mode(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "mock"
    assert body["candle_counts"], "预热后必须已经有缓存 K 线"
    assert body["feed"]["connected"] is True


def test_config_lists_supported_streams(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["default_symbol"]
    assert body["default_interval"] in body["intervals"]
    assert all("@" in item for item in body["stream_keys"])
    assert body["fee_rate"] > 0


def test_config_exposes_strategy_catalog(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["default_strategy"] == "grid"
    assert [item["key"] for item in body["strategies"]] == ["grid", "martingale", "trend"]
    for strategy in body["strategies"]:
        assert strategy["label"] and strategy["params"]
        for param in strategy["params"]:
            assert {"key", "label", "default", "min", "max", "help"} <= set(param)


def test_config_exposes_execution_capabilities(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["trading_modes"] == ["spot", "futures"]
    assert [param["key"] for param in body["shared_params"]] == [
        "mode",
        "leverage",
        "funding_rate_8h",
    ]
    assert "1y" in body["intervals"]
    assert "1w" in body["intervals"] and "1M" in body["intervals"]


def test_latest_data_derives_yearly_candles(client: TestClient) -> None:
    response = client.get(
        "/api/latest_data", params={"symbol": "BTCUSDT", "interval": "1y", "limit": 30}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interval"] == "1y"
    assert body["base_interval"] == "1M"
    assert body["count"] >= 1
    assert body["price"] > 0
    # 年 K 的时间戳必须落在 1 月 1 日 00:00 UTC
    for candle in body["candles"]:
        assert datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).month == 1
        assert datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).day == 1


def test_backtest_on_yearly_interval(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={
            "strategy": "grid",
            "symbol": "BTCUSDT",
            "interval": "1y",
            "limit": 20,
            "params": {"grid_count": 3},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "grid"
    assert body["summary"]["sample_bars"] >= 2


def test_latest_data_returns_candles(client: TestClient) -> None:
    body = client.get("/api/latest_data", params={"symbol": "BTCUSDT", "interval": "1m", "limit": 50}).json()
    assert body["symbol"] == "BTCUSDT"
    assert body["interval"] == "1m"
    assert 1 <= body["count"] <= 50
    assert body["price"] > 0
    candle = body["candles"][-1]
    assert set(candle) >= {"t", "o", "h", "l", "c"}
    assert candle["h"] >= candle["c"] >= candle["l"]
    assert body["stale"] is False


def test_latest_data_rejects_unknown_stream(client: TestClient) -> None:
    response = client.get("/api/latest_data", params={"symbol": "FAKEUSDT", "interval": "1m"})
    assert response.status_code == 404
    assert "未订阅" in response.json()["detail"]


def test_backtest_endpoint_returns_full_report(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={
            "symbol": "BTCUSDT",
            "interval": "1m",
            "grid_count": 15,
            "initial_capital": 5000,
            "fee_rate": 0.001,
            "limit": 250,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["sample_bars"] == 250
    assert body["summary"]["initial_capital"] == 5000
    assert body["grid"]["count"] == 15
    assert len(body["grid"]["levels"]) == 16
    assert isinstance(body["trades"], list)
    assert "max_drawdown" in body["summary"] and "sharpe" in body["summary"]


def test_backtest_endpoint_rejects_invalid_grid(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={"symbol": "BTCUSDT", "interval": "1m", "lower": 120, "upper": 100, "grid_count": 10},
    )
    assert response.status_code == 400
    assert response.json()["detail"]


def test_backtest_supports_martingale(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={
            "strategy": "martingale",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": 200,
            "params": {
                "initial_order_usdt": 100,
                "drop_pct": 0.01,
                "multiplier": 2,
                "take_profit_pct": 0.005,
                "max_entries": 4,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "martingale"
    assert body["params"]["max_entries"] == 4
    assert body["highlight"]["label"] == "最大加仓层数"
    assert body["summary"]["sample_bars"] == 200


def test_backtest_supports_trend(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={
            "strategy": "trend",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": 200,
            "params": {"fast_ma": 5, "slow_ma": 20, "stop_loss_pct": 0.02},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "trend"
    assert [line["name"] for line in body["overlays"]["lines"]] == ["SMA5", "SMA20"]
    assert body["params"]["fast_ma"] == 5


def test_backtest_rejects_unknown_strategy(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={"strategy": "quantum", "symbol": "BTCUSDT", "interval": "1m"},
    )
    assert response.status_code == 400
    assert "未知策略" in response.json()["detail"]


def test_backtest_supports_futures_leverage(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={
            "strategy": "trend",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": 250,
            "params": {
                "fast_ma": 5,
                "slow_ma": 20,
                "mode": "futures",
                "leverage": 5,
                "funding_rate_8h": 0.0001,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution"]["mode"] == "futures"
    assert body["execution"]["leverage"] == 5
    assert body["execution"]["direction"] == "long_short"
    assert body["execution"]["fee_rate"] == pytest.approx(0.0005), "合约模式默认 taker 0.05%"
    assert body["summary"]["leverage"] == 5
    assert body["params"]["mode"] == "futures"


def test_sentiment_endpoint_returns_composite_score(client: TestClient) -> None:
    response = client.get("/api/sentiment", params={"symbol": "BTCUSDT"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert 0 <= body["score"] <= 100
    assert body["label"]
    assert body["offline"] is True  # Mock 模式：不访问外部接口
    assert len(body["history"]) == 30
    assert "fear_greed" in body["components"]
    assert body["advice"]


def test_news_endpoint_returns_items(client: TestClient) -> None:
    response = client.get("/api/news", params={"limit": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 3
    assert body["offline"] is True
    for item in body["items"]:
        assert {"title", "link", "source"} <= set(item)
