"""绩效指标计算：最大回撤、夏普比率、年化收益、胜率、盈亏比。

刻意只用标准库（math / statistics），不依赖 numpy / pandas：
回测内核是纯函数，老师拿着计算器也能对照检查。
"""

from __future__ import annotations

import math
from statistics import fmean, stdev
from typing import Iterable, Sequence

from core.timeframe import bars_per_year as _bars_per_year

MAX_ANNUALIZED_RETURN = 100.0
MIN_ANNUALIZED_RETURN = -0.9999


def max_drawdown(equity: Sequence[float]) -> float:
    """最大回撤（正数，0.2 表示最大回撤 20%）。"""
    peak: float | None = None
    worst = 0.0
    for value in equity:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = max(worst, (peak - value) / peak)
    return float(worst)


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """逐点回撤序列（负数，-0.2 表示距峰值回撤 20%）。"""
    peak: float | None = None
    out: list[float] = []
    for value in equity:
        if peak is None or value > peak:
            peak = value
        out.append((value - peak) / peak if peak else 0.0)
    return out


def sharpe_ratio(returns: Sequence[float], interval: str) -> float:
    """年化夏普比率（无风险利率按 0 处理）。

    注意：加密市场 7×24 交易，年化系数按 365 天计算。
    """
    clean = [r for r in returns if math.isfinite(r)]
    if len(clean) < 3:
        return 0.0
    mean = fmean(clean)
    sd = stdev(clean)
    if sd <= 1e-12 or not math.isfinite(sd):
        return 0.0
    return float(mean / sd * math.sqrt(_bars_per_year(interval)))


def annualized_return(total_return: float, n_bars: int, interval: str) -> float:
    """按复利把区间收益折算成年化收益。

    样本不足一年的短周期回测折算出来会很夸张（几百倍），
    这是数学事实而非引擎错误，因此这里做上下限截断，前端会同时给出提示。
    """
    if n_bars <= 0:
        return 0.0
    years = n_bars / _bars_per_year(interval)
    if years <= 0:
        return 0.0
    base = 1.0 + total_return
    if base <= 0:
        return MIN_ANNUALIZED_RETURN
    try:
        value = base ** (1.0 / years) - 1.0
    except OverflowError:
        return MAX_ANNUALIZED_RETURN
    if not math.isfinite(value):
        return MAX_ANNUALIZED_RETURN
    return float(max(MIN_ANNUALIZED_RETURN, min(value, MAX_ANNUALIZED_RETURN)))


def win_rate(pnls: Iterable[float]) -> float:
    """胜率 = 盈利平仓次数 / 平仓总次数。"""
    values = list(pnls)
    if not values:
        return 0.0
    return sum(1 for pnl in values if pnl > 0) / len(values)


def profit_factor(pnls: Iterable[float]) -> float | None:
    """盈亏比 = 总盈利 / 总亏损。没有亏损单时返回 None（无穷大不可直接展示）。"""
    values = list(pnls)
    gross_profit = sum(pnl for pnl in values if pnl > 0)
    gross_loss = -sum(pnl for pnl in values if pnl < 0)
    if gross_loss <= 1e-12:
        return None
    return gross_profit / gross_loss


def summarize_trade_pnls(pnls: Sequence[float], interval: str, n_bars: int, total_return: float) -> dict:
    """把平仓盈亏汇总成指标字典（供 summary 复用）。"""
    values = list(pnls)
    wins = [pnl for pnl in values if pnl > 0]
    losses = [pnl for pnl in values if pnl < 0]
    return {
        "closed_trade_count": len(values),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate(values),
        "profit_factor": profit_factor(values),
        "avg_trade_pnl": fmean(values) if values else 0.0,
        "best_trade_pnl": max(values) if values else 0.0,
        "worst_trade_pnl": min(values) if values else 0.0,
        "annualized_return": annualized_return(total_return, n_bars, interval),
    }
