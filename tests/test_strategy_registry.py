"""策略注册表契约测试：三种策略必须输出同一套结构，前端才能只写一套渲染。"""

from __future__ import annotations

import pytest

from core.strategy_registry import STRATEGY_KEYS, catalog, get_spec, run_backtest


def candles_from_prices(prices: list[float]) -> list[dict]:
    rows: list[dict] = []
    previous = prices[0]
    for index, price in enumerate(prices):
        rows.append(
            {
                "t": 1_700_000_000_000 + index * 60_000,
                "o": previous,
                "h": max(previous, price) * 1.0005,
                "l": min(previous, price) * 0.9995,
                "c": price,
                "v": 1.0,
            }
        )
        previous = price
    return rows


CANDLES = candles_from_prices(
    [100.0 + index * 0.3 for index in range(60)]
    + [118.0 - index * 0.5 for index in range(40)]
    + [98.0 + (index % 5) * 0.4 for index in range(60)]
)

SUMMARY_KEYS = {
    "initial_capital",
    "final_equity",
    "net_pnl",
    "total_return",
    "max_drawdown",
    "sharpe",
    "win_rate",
    "trade_count",
    "buy_count",
    "sell_count",
    "total_fees",
    "profit_factor",
    "open_position_qty",
    "sample_bars",
    "start_time",
    "end_time",
}


def test_catalog_describes_every_strategy() -> None:
    entries = catalog()
    assert [entry["key"] for entry in entries] == list(STRATEGY_KEYS)
    assert set(STRATEGY_KEYS) == {"grid", "martingale", "trend"}

    for entry in entries:
        assert entry["label"] and entry["icon"] and entry["tagline"]
        for param in entry["params"]:
            assert param["label"] and param["help"]
            assert param["min"] <= param["default"] <= param["max"]
            assert param["kind"] in ("int", "float")


@pytest.mark.parametrize("strategy", STRATEGY_KEYS)
def test_every_strategy_returns_same_contract(strategy: str) -> None:
    result = run_backtest(
        strategy,
        CANDLES,
        {"initial_capital": 10_000.0, "fee_rate": 0.001, "lower": 90.0, "upper": 130.0, "grid_count": 15},
    )

    assert result["strategy"] == strategy
    assert result["strategy_label"]
    assert result["highlight"]["label"] and str(result["highlight"]["value"])
    assert SUMMARY_KEYS <= set(result["summary"])
    assert len(result["equity_curve"]) == len(CANDLES)
    assert result["summary"]["final_equity"] == pytest.approx(result["equity_curve"][-1]["equity"])
    assert result["summary"]["sample_bars"] == len(CANDLES)
    assert result["meta"]["assumptions"]
    assert isinstance(result["overlays"], dict)
    assert "grid_levels" in result["overlays"] and "lines" in result["overlays"]

    timestamps = [trade["t"] for trade in result["trades"]]
    assert timestamps == sorted(timestamps), "交易流水必须按时间升序"
    fee_sum = sum(trade["fee"] for trade in result["trades"])
    assert fee_sum == pytest.approx(result["summary"]["total_fees"], rel=1e-9)


def test_grid_bounds_are_derived_when_missing() -> None:
    result = run_backtest("grid", CANDLES, {"grid_count": 10})
    last_close = CANDLES[-1]["c"]
    lower = result["params"]["lower"]
    upper = result["params"]["upper"]

    assert lower == pytest.approx(last_close * 0.97, rel=1e-6)
    assert upper == pytest.approx(last_close * 1.03, rel=1e-6)
    assert len(result["grid"]["levels"]) == 11


def test_martingale_and_trend_use_their_own_params() -> None:
    martingale = run_backtest(
        "martingale", CANDLES, {"initial_order_usdt": 250.0, "multiplier": 1.5, "max_entries": 4}
    )
    assert martingale["params"]["initial_order_usdt"] == 250.0
    assert martingale["params"]["multiplier"] == 1.5
    assert martingale["summary"]["required_capital"] == pytest.approx(250.0 * (1 + 1.5 + 2.25 + 3.375))

    trend = run_backtest("trend", CANDLES, {"fast_ma": 4, "slow_ma": 12, "stop_loss_pct": 0.03})
    assert trend["params"]["fast_ma"] == 4
    assert trend["params"]["slow_ma"] == 12
    assert [line["name"] for line in trend["overlays"]["lines"]] == ["SMA4", "SMA12"]


def test_unknown_strategy_raises_readable_error() -> None:
    with pytest.raises(ValueError, match="未知策略"):
        get_spec("martingale_v2")
    with pytest.raises(ValueError, match="未知策略"):
        run_backtest("martingale_v2", CANDLES, {})


def test_futures_mode_is_propagated_to_result() -> None:
    result = run_backtest(
        "trend",
        CANDLES,
        {"mode": "futures", "leverage": 5, "funding_rate_8h": 0.0001, "fast_ma": 5, "slow_ma": 20},
    )
    assert result["execution"] == {
        "mode": "futures",
        "leverage": 5.0,
        "fee_rate": 0.0005,
        "funding_rate_8h": 0.0001,
        "direction": "long_short",
    }
    assert result["params"]["mode"] == "futures"
    assert result["params"]["leverage"] == 5
    assert result["summary"]["leverage"] == 5


def test_spot_mode_forces_leverage_one() -> None:
    result = run_backtest("trend", CANDLES, {"mode": "spot", "leverage": 10})
    assert result["execution"]["leverage"] == 1.0
    assert result["summary"]["liquidation_count"] == 0
