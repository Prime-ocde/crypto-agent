"""趋势跟踪策略单测：执行时点、止损、空仓等待、样本不足。"""

from __future__ import annotations

import pytest

from core.trend import TrendParams, run_trend_backtest


def candles_from_prices(
    prices: list[float],
    start_ms: int = 1_700_000_000_000,
    step_ms: int = 60_000,
    wick: float = 0.0005,
) -> list[dict]:
    rows: list[dict] = []
    previous = prices[0]
    for index, price in enumerate(prices):
        rows.append(
            {
                "t": start_ms + index * step_ms,
                "o": previous,
                "h": max(previous, price) * (1.0 + wick),
                "l": min(previous, price) * (1.0 - wick),
                "c": price,
                "v": 1.0,
            }
        )
        previous = price
    return rows


def test_uptrend_buys_and_holds() -> None:
    rows = candles_from_prices([100.0 + index * 0.5 for index in range(120)])
    result = run_trend_backtest(rows, TrendParams(fast_ma=5, slow_ma=20, stop_loss_pct=0.05))

    summary = result["summary"]
    assert summary["buy_count"] >= 1
    assert summary["trade_count"] >= 1
    assert summary["total_return"] > 0, "单边上涨行情里趋势策略应当赚钱"
    assert summary["open_position_qty"] > 0, "行情没转空，应当仍然持仓"
    assert len(result["overlays"]["lines"]) == 2
    assert result["overlays"]["lines"][0]["name"] == "SMA5"


def test_entries_and_exits_execute_at_candle_open() -> None:
    """信号在下一根开盘执行：成交价必须等于该 K 线的开盘价（杜绝未来函数）。"""
    rows = candles_from_prices([100.0 + 6.0 * ((-1) ** index) * 0.05 + index * 0.4 for index in range(150)])
    by_time = {row["t"]: row for row in rows}
    result = run_trend_backtest(rows, TrendParams(fast_ma=4, slow_ma=12, stop_loss_pct=0.0))

    open_executed = [
        trade for trade in result["trades"] if trade["reason"] in ("均线多头开仓", "均线转空平仓")
    ]
    assert open_executed, "构造的行情应当产生均线信号"
    for trade in open_executed:
        assert trade["price"] == pytest.approx(by_time[trade["t"]]["o"], rel=1e-12)


def test_first_trade_waits_for_slow_ma_warmup() -> None:
    rows = candles_from_prices([100.0 + index * 0.6 for index in range(120)])
    result = run_trend_backtest(rows, TrendParams(fast_ma=5, slow_ma=30))
    first_trade_t = result["trades"][0]["t"]
    assert first_trade_t >= rows[30]["t"], "慢线没算出来之前不可能有信号"


def test_stop_loss_triggers_on_crash() -> None:
    prices = [100.0 + index * 1.0 for index in range(40)]
    prices.append(prices[-1] * 0.70)  # 一根断崖，跌破持仓成本
    rows = candles_from_prices(prices, wick=0.0)
    result = run_trend_backtest(
        rows, TrendParams(fast_ma=3, slow_ma=8, stop_loss_pct=0.02, take_profit_pct=0.0)
    )

    stop_trades = [trade for trade in result["trades"] if trade["reason"] == "止损"]
    assert stop_trades, "断崖下跌必须触发止损"
    assert stop_trades[0]["pnl"] < 0
    assert result["summary"]["stop_loss_count"] == len(stop_trades)


def test_downtrend_stays_flat() -> None:
    rows = candles_from_prices([200.0 - index * 0.8 for index in range(120)])
    result = run_trend_backtest(rows, TrendParams(fast_ma=5, slow_ma=20))

    assert result["summary"]["trade_count"] == 0
    assert result["summary"]["total_return"] == pytest.approx(0.0)
    assert any("空仓" in warning for warning in result["warnings"])


def test_downtrend_opens_short_in_futures_double_sided_mode() -> None:
    rows = candles_from_prices([200.0 - index * 0.8 for index in range(120)])
    result = run_trend_backtest(
        rows,
        TrendParams(
            fast_ma=5,
            slow_ma=20,
            mode="futures",
            leverage=5,
            direction="long_short",
            stop_loss_pct=0.05,
        ),
    )

    shorts = [trade for trade in result["trades"] if trade.get("position") == "short"]
    assert shorts, "空头排列时应当开空"
    assert any(trade["side"] == "sell" and trade["reason"] == "均线空头开仓" for trade in shorts)
    assert result["summary"]["total_return"] > 0, "单边下跌里做空应当盈利"
    assert result["meta"]["direction"] == "long_short"


def test_short_gets_liquidated_on_pump() -> None:
    prices = [200.0 - index * 0.5 for index in range(60)]
    prices += [170.0, 225.0, 230.0]  # 单根暴力拉升，空头来不及按均线离场
    rows = candles_from_prices(prices)
    result = run_trend_backtest(
        rows,
        TrendParams(
            fast_ma=3,
            slow_ma=8,
            mode="futures",
            leverage=20,
            direction="long_short",
            stop_loss_pct=0.0,
        ),
    )
    assert result["summary"]["liquidation_count"] >= 1
    assert any("强平" in trade["reason"] for trade in result["trades"])


def test_spot_rejects_double_sided_direction() -> None:
    with pytest.raises(ValueError, match="现货模式不支持做空"):
        TrendParams(mode="spot", direction="long_short").validate()


def test_insufficient_sample_warns_instead_of_crashing() -> None:
    rows = candles_from_prices([100.0 + index for index in range(10)])
    result = run_trend_backtest(rows, TrendParams(fast_ma=5, slow_ma=30))

    assert result["summary"]["trade_count"] == 0
    assert result["summary"]["sample_bars"] == 10
    assert any("样本不足" in warning for warning in result["warnings"])


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValueError, match="慢线周期必须大于快线周期"):
        TrendParams(fast_ma=20, slow_ma=10).validate()
    with pytest.raises(ValueError, match="仓位比例"):
        TrendParams(position_pct=0).validate()
    with pytest.raises(ValueError, match="止损比例"):
        TrendParams(stop_loss_pct=0.6).validate()
    with pytest.raises(ValueError, match="手续费率"):
        TrendParams(fee_rate=0.2).validate()
