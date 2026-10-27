"""统一的绩效汇总：三种策略输出同一套指标契约，前端只写一套渲染。

网格 / 马丁格尔 / 趋势 的回测结果都长这样：
```
{
  "strategy": "grid" | "martingale" | "trend",
  "summary": {total_return, max_drawdown, sharpe, win_rate, ...},
  "equity_curve": [{"t", "time", "equity", "price"}],
  "trades": [...],
  "overlays": {"grid_levels": [...], "lines": [...]},
  "highlight": {"label", "value", "help"},
  "warnings": [...],
  "meta": {"engine", "assumptions", ...}
}
```
这是"契约先行"：新增策略只要满足这个契约，前端零改动。
"""

from __future__ import annotations

from typing import Any

from core import metrics


def build_summary(
    *,
    initial_capital: float,
    equity_curve: list[dict],
    trades: list[dict],
    interval: str,
    open_position_qty: float,
    open_position_value: float,
    total_fees: float,
    realized_pnl: float,
    extras: dict[str, Any] | None = None,
) -> dict:
    equities = [float(point["equity"]) for point in equity_curve] or [float(initial_capital)]
    final_equity = equities[-1]
    total_return = final_equity / initial_capital - 1.0
    returns = [
        equities[index] / equities[index - 1] - 1.0
        for index in range(1, len(equities))
        if equities[index - 1] > 0
    ]
    # 已实现盈亏：多空两侧的平仓单都要算（空头平仓是 buy，不能只统计 side=="sell"）
    closed_pnls = [
        float(trade["pnl"]) for trade in trades if trade.get("pnl") is not None
    ]

    summary = {
        "initial_capital": float(initial_capital),
        "final_equity": final_equity,
        "net_pnl": final_equity - initial_capital,
        "total_return": total_return,
        "max_drawdown": metrics.max_drawdown(equities),
        "sharpe": metrics.sharpe_ratio(returns, interval),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": final_equity - initial_capital - realized_pnl,
        "total_fees": total_fees,
        "fee_drag": total_fees / initial_capital if initial_capital else 0.0,
        "trade_count": len(trades),
        "buy_count": sum(1 for trade in trades if trade.get("side") == "buy"),
        "sell_count": sum(1 for trade in trades if trade.get("side") == "sell"),
        "open_position_qty": open_position_qty,
        "open_position_value": open_position_value,
        "sample_bars": len(equity_curve),
        "start_time": equity_curve[0].get("time", "") if equity_curve else "",
        "end_time": equity_curve[-1].get("time", "") if equity_curve else "",
    }
    summary.update(metrics.summarize_trade_pnls(closed_pnls, interval, len(equity_curve), total_return))
    # 波动率拖累：算术均值夏普可以为正，几何复利却可能把账户打光（高波动下均值被方差吃掉）
    summary["volatility_drag"] = bool(total_return < -0.2 and summary.get("sharpe", 0.0) > 0)
    if extras:
        summary.update(extras)
    return summary


def equity_point(ts: int, time_text: str, equity: float, price: float) -> dict:
    return {"t": ts, "time": time_text, "equity": equity, "price": price}
