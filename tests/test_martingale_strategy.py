"""马丁格尔策略单测：几何加仓、止盈闭环、层数上限、资金断裂告警。"""

from __future__ import annotations

import pytest

from core.martingale import MartingaleParams, run_martingale_backtest


def candles_from_prices(
    prices: list[float],
    start_ms: int = 1_700_000_000_000,
    step_ms: int = 60_000,
) -> list[dict]:
    rows: list[dict] = []
    previous = prices[0]
    for index, price in enumerate(prices):
        rows.append(
            {
                "t": start_ms + index * step_ms,
                "o": previous,
                "h": max(previous, price),
                "l": min(previous, price),
                "c": price,
                "v": 1.0,
            }
        )
        previous = price
    return rows


def test_adds_are_geometric_after_each_drop() -> None:
    prices = [100.0, 97.9, 95.9, 94.0, 99.5, 101.0]
    rows = candles_from_prices(prices)
    result = run_martingale_backtest(
        rows,
        MartingaleParams(
            initial_order_usdt=100.0,
            drop_pct=0.02,
            multiplier=2.0,
            take_profit_pct=0.01,
            max_entries=5,
            initial_capital=10_000.0,
        ),
    )

    buys = [trade for trade in result["trades"] if trade["side"] == "buy"]
    assert len(buys) >= 3, "连续下跌应当触发多次加仓"
    assert buys[1]["notional"] == pytest.approx(buys[0]["notional"] * 2, rel=1e-6)
    assert buys[2]["notional"] == pytest.approx(buys[1]["notional"] * 2, rel=1e-6)
    assert buys[0]["reason"] == "建仓"
    assert "加仓" in buys[1]["reason"]


def test_take_profit_closes_whole_position_and_restarts() -> None:
    prices = [100.0, 97.9, 95.9, 94.0, 96.0, 99.5, 100.5]
    rows = candles_from_prices(prices)
    result = run_martingale_backtest(
        rows,
        MartingaleParams(initial_order_usdt=100.0, drop_pct=0.02, multiplier=2.0,
                         take_profit_pct=0.01, max_entries=6, initial_capital=10_000.0),
    )

    sells = [trade for trade in result["trades"] if trade["side"] == "sell"]
    assert sells, "价格回到均价上方应当止盈"
    assert sells[0]["reason"] == "止盈"
    assert sells[0]["pnl"] > 0, "止盈交易必须盈利（已扣双边手续费）"

    sell_time = sells[0]["t"]
    rebuys = [trade for trade in result["trades"] if trade["side"] == "buy" and trade["t"] > sell_time]
    assert rebuys, "止盈后应当在下一根 K 线重新建仓（马丁格尔的循环特征）"


def test_max_entries_caps_layers() -> None:
    prices = [100.0 - index * 1.5 for index in range(30)]
    rows = candles_from_prices(prices)
    result = run_martingale_backtest(
        rows,
        MartingaleParams(initial_order_usdt=50.0, drop_pct=0.01, multiplier=2.0,
                         take_profit_pct=0.5, max_entries=3, initial_capital=10_000.0),
    )

    assert result["summary"]["layers_max"] <= 3
    assert any("最大加仓层数" in warning for warning in result["warnings"])


def test_insufficient_capital_warns_and_skips_adds() -> None:
    prices = [100.0 - index * 2.0 for index in range(20)]
    rows = candles_from_prices(prices)
    result = run_martingale_backtest(
        rows,
        MartingaleParams(initial_order_usdt=100.0, drop_pct=0.01, multiplier=2.0,
                         take_profit_pct=0.2, max_entries=6, initial_capital=250.0),
    )

    assert result["summary"]["skipped_adds"] > 0
    assert result["summary"]["required_capital"] > 250.0
    assert any("资金不足" in warning for warning in result["warnings"])
    assert any("几何级数" in warning for warning in result["warnings"])


def test_accounting_uses_shared_report_contract() -> None:
    prices = [100.0, 98.0, 96.0, 99.0, 101.0, 97.0, 95.0]
    rows = candles_from_prices(prices)
    result = run_martingale_backtest(
        rows, MartingaleParams(initial_order_usdt=200.0, max_entries=5, initial_capital=5_000.0)
    )
    summary = result["summary"]

    assert summary["sample_bars"] == len(rows)
    assert summary["final_equity"] == pytest.approx(result["equity_curve"][-1]["equity"])
    fee_sum = sum(trade["fee"] for trade in result["trades"])
    assert fee_sum == pytest.approx(summary["total_fees"], rel=1e-9)
    assert summary["trade_count"] == len(result["trades"])


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError, match="加仓跌幅"):
        MartingaleParams(drop_pct=0).validate()
    with pytest.raises(ValueError, match="加仓倍数"):
        MartingaleParams(multiplier=0.5).validate()
    with pytest.raises(ValueError, match="最大加仓层数"):
        MartingaleParams(max_entries=1).validate()
    with pytest.raises(ValueError, match="首次建仓金额不能超过初始资金"):
        MartingaleParams(initial_order_usdt=20_000, initial_capital=1_000).validate()
