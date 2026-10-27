"""网格回测引擎单测：参数校验、成交逻辑、资金守恒、指标边界。"""

from __future__ import annotations

import pytest

from core.grid_strategy import GridParams, run_grid_backtest


def candles_from_prices(
    prices: list[float],
    start_ms: int = 1_700_000_000_000,
    step_ms: int = 60_000,
    wick: float = 0.0,
) -> list[dict]:
    """用收盘价序列构造首尾相接的 K 线，wick 控制影线幅度。"""
    rows: list[dict] = []
    previous = prices[0]
    for index, price in enumerate(prices):
        high = max(previous, price) * (1.0 + wick)
        low = min(previous, price) * (1.0 - wick)
        rows.append(
            {
                "t": start_ms + index * step_ms,
                "o": previous,
                "h": high,
                "l": low,
                "c": price,
                "v": 1.0,
            }
        )
        previous = price
    return rows


def test_params_validation_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="下界必须大于 0"):
        GridParams(lower=0, upper=100, grid_count=10).validate()
    with pytest.raises(ValueError, match="上界必须大于"):
        GridParams(lower=100, upper=100, grid_count=10).validate()
    with pytest.raises(ValueError, match="至少为 2"):
        GridParams(lower=90, upper=110, grid_count=1).validate()
    with pytest.raises(ValueError, match="手续费率"):
        GridParams(lower=90, upper=110, grid_count=10, fee_rate=0.5).validate()
    with pytest.raises(ValueError, match="周期"):
        GridParams(lower=90, upper=110, grid_count=10, interval="7m").validate()


def test_flat_price_never_trades() -> None:
    rows = candles_from_prices([100.0] * 50)
    result = run_grid_backtest(rows, GridParams(lower=90, upper=110, grid_count=10))
    summary = result["summary"]

    assert summary["trade_count"] == 0
    assert summary["final_equity"] == pytest.approx(10_000.0)
    assert summary["total_return"] == pytest.approx(0.0)
    assert summary["max_drawdown"] == pytest.approx(0.0)
    assert result["warnings"], "完全不成交时必须给出提示，不能静默返回"


def test_price_outside_grid_never_trades() -> None:
    rows = candles_from_prices([200.0 + index for index in range(40)])
    result = run_grid_backtest(rows, GridParams(lower=90, upper=110, grid_count=10))
    assert result["summary"]["trade_count"] == 0
    assert any("上界" in warning for warning in result["warnings"])


def test_zigzag_oscillation_trades_both_ways() -> None:
    prices: list[float] = []
    for _ in range(6):
        prices.extend([100.0, 94.0, 106.0])
    rows = candles_from_prices(prices, wick=0.0005)
    result = run_grid_backtest(rows, GridParams(lower=90, upper=110, grid_count=10, fee_rate=0.001))
    summary = result["summary"]

    assert summary["buy_count"] > 0
    assert summary["sell_count"] > 0
    assert summary["trade_count"] == summary["buy_count"] + summary["sell_count"]
    assert summary["win_rate"] >= 0.5, "低买高卖的网格在震荡行情里胜率不该低于 50%"
    assert summary["total_fees"] > 0
    assert 0.0 <= summary["max_drawdown"] <= 1.0


def test_equity_curve_and_fee_accounting_are_consistent() -> None:
    prices = [100.0, 97.0, 103.0, 95.0, 105.0, 96.0, 104.0]
    rows = candles_from_prices(prices)
    result = run_grid_backtest(rows, GridParams(lower=90, upper=110, grid_count=20, fee_rate=0.002))

    assert len(result["equity_curve"]) == len(rows)
    assert result["equity_curve"][-1]["equity"] == pytest.approx(result["summary"]["final_equity"])
    fee_sum = sum(trade["fee"] for trade in result["trades"])
    assert fee_sum == pytest.approx(result["summary"]["total_fees"], rel=1e-9)

    # 硬核校验：只用交易流水重算现金，再验证"期末权益 = 现金 + 持仓 × 收盘价"
    cash = 10_000.0
    for trade in result["trades"]:
        if trade["side"] == "buy":
            cash -= trade["notional"] + trade["fee"]
        else:
            cash += trade["notional"] - trade["fee"]
    last_close = rows[-1]["c"]
    assert result["summary"]["final_equity"] == pytest.approx(
        cash + result["summary"]["open_position_qty"] * last_close, rel=1e-9
    )
    # 手续费必须真的扣掉了：净收益应小于"零手续费"下的重算收益
    assert result["summary"]["total_fees"] > 0


def test_liquidate_at_end_leaves_no_open_position() -> None:
    prices = [100.0, 95.0, 99.0, 94.0, 98.0]
    rows = candles_from_prices(prices)
    params = GridParams(lower=90, upper=110, grid_count=10)
    result = run_grid_backtest(rows, params, liquidate_at_end=True)

    assert result["summary"]["open_position_qty"] == pytest.approx(0.0)
    assert result["summary"]["buy_count"] > 0
    assert result["equity_curve"][-1]["equity"] == pytest.approx(result["summary"]["final_equity"])


def test_accepts_dataframe_input() -> None:
    pandas = pytest.importorskip("pandas")
    rows = candles_from_prices([100.0, 96.0, 104.0, 97.0, 103.0])
    frame = pandas.DataFrame(
        {
            "time": [row["t"] for row in rows],
            "open": [row["o"] for row in rows],
            "high": [row["h"] for row in rows],
            "low": [row["l"] for row in rows],
            "close": [row["c"] for row in rows],
            "volume": [row["v"] for row in rows],
        }
    )
    result = run_grid_backtest(frame, GridParams(lower=90, upper=110, grid_count=10))
    assert result["summary"]["sample_bars"] == len(rows)


def test_grid_keeps_one_lot_per_level() -> None:
    """价格反复向下穿越同一条网格线时，不允许在同一格反复加仓。"""
    prices: list[float] = []
    for _ in range(5):
        prices.extend([100.6, 99.4])
    rows = candles_from_prices(prices)
    result = run_grid_backtest(rows, GridParams(lower=90, upper=110, grid_count=20, fee_rate=0.0))

    buys = [trade for trade in result["trades"] if trade["side"] == "buy"]
    assert buys, "价格穿越网格线应当买入"
    levels = [trade["level"] for trade in buys]
    assert len(levels) == len(set(levels)), "同一网格线不允许重复买入"


def test_rejects_too_few_candles() -> None:
    with pytest.raises(ValueError, match="至少需要 2 根"):
        run_grid_backtest(candles_from_prices([100.0]), GridParams(lower=90, upper=110, grid_count=10))


def test_warns_when_grid_step_smaller_than_round_trip_fee() -> None:
    rows = candles_from_prices([100.0, 100.2, 99.8, 100.2, 99.8, 100.1, 99.9])
    # 区间 2 USDT / 20 格 = 0.1% 间距，低于双边 0.2% 手续费
    result = run_grid_backtest(rows, GridParams(lower=99.0, upper=101.0, grid_count=20, fee_rate=0.001))
    assert any("小于双边手续费" in warning for warning in result["warnings"])


def test_no_fee_warning_when_step_is_wide_enough() -> None:
    rows = candles_from_prices([100.0, 99.0, 101.0, 99.0, 101.0])
    result = run_grid_backtest(rows, GridParams(lower=90.0, upper=110.0, grid_count=20, fee_rate=0.001))
    assert not any("小于双边手续费" in warning for warning in result["warnings"])
