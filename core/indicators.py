"""技术指标纯函数：MA / BOLL / MACD / RSI / KDJ。

## 约定

- 输入是**收盘价序列**（KDJ 额外需要 high/low），输出与输入**等长**，
  预热不足的位置是 `None` —— 前端按索引取点即可，不需要再对齐时间戳；
- 全部用简单移动平均 / Wilder 平滑等**教科书口径**，不做"优化"，
  这样和 TradingView、同花顺对得上，讲课不会被问倒；
- 除零一律有明确定义（见各函数文档），不会抛异常、不会产出 NaN。

## 边界口径（重要）

- RSI：全涨 → 100，全跌 → 0，完全不动 → 50；
- KDJ：`high == low`（一字板/数据缺失）时 RSV 记 50；
- BOLL：标准差用**总体标准差**（除以 n），与 TradingView 的 `stdev` 一致。
"""

from __future__ import annotations

import math
from typing import Sequence

Number = float | None


def _clean(values: Sequence[float]) -> list[float]:
    return [float(value) for value in values]


def sma(values: Sequence[float], period: int) -> list[Number]:
    """简单移动平均。"""
    if period <= 0:
        raise ValueError("周期必须为正整数")
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    window = 0.0
    for index, value in enumerate(data):
        window += value
        if index >= period:
            window -= data[index - period]
        if index >= period - 1:
            out[index] = window / period
    return out


def ema(values: Sequence[float], period: int) -> list[Number]:
    """指数移动平均：第一根用前 period 根的 SMA 做种子，之后递推。"""
    if period <= 0:
        raise ValueError("周期必须为正整数")
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    if len(data) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = sum(data[:period]) / period
    out[period - 1] = seed
    prev = seed
    for index in range(period, len(data)):
        prev = alpha * data[index] + (1.0 - alpha) * prev
        out[index] = prev
    return out


def bollinger(
    values: Sequence[float],
    period: int = 20,
    mult: float = 2.0,
) -> dict[str, list[Number]]:
    """布林带：中轨 = SMA，上下轨 = 中轨 ± mult × 总体标准差。"""
    if mult <= 0:
        raise ValueError("标准差倍数必须大于 0")
    data = _clean(values)
    mid = sma(data, period)
    upper: list[Number] = [None] * len(data)
    lower: list[Number] = [None] * len(data)
    for index in range(period - 1, len(data)):
        window = data[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        deviation = math.sqrt(variance)
        upper[index] = mean + mult * deviation
        lower[index] = mean - mult * deviation
    return {"mid": mid, "upper": upper, "lower": lower}


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[Number]]:
    """MACD：DIF = EMA(fast) − EMA(slow)，DEA = EMA(DIF, signal)，柱 = 2×(DIF − DEA)。

    柱状图乘 2 是国内软件（同花顺/通达信）的口径，和 TradingView 的 1 倍不同，
    两侧都会在页面上标注，避免"同一个 MACD 两个值"的争论。
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("周期必须为正整数")
    if fast >= slow:
        raise ValueError("快线周期必须小于慢线周期")
    data = _clean(values)
    fast_ema = ema(data, fast)
    slow_ema = ema(data, slow)
    dif: list[Number] = [None] * len(data)
    for index in range(len(data)):
        left, right = fast_ema[index], slow_ema[index]
        if left is not None and right is not None:
            dif[index] = left - right

    valid = [index for index, value in enumerate(dif) if value is not None]
    dea: list[Number] = [None] * len(data)
    hist: list[Number] = [None] * len(data)
    if len(valid) >= signal:
        compact = [dif[index] for index in valid]  # type: ignore[misc]
        smoothed = ema([float(value) for value in compact], signal)
        for offset, index in enumerate(valid):
            value = smoothed[offset]
            if value is not None:
                dea[index] = value
                hist[index] = 2.0 * (float(dif[index]) - value)
    return {"dif": dif, "dea": dea, "hist": hist}


def rsi(values: Sequence[float], period: int = 14) -> list[Number]:
    """RSI（Wilder 平滑）：平均涨幅 / 平均涨幅+平均跌幅。"""
    if period <= 0:
        raise ValueError("周期必须为正整数")
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    if len(data) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = data[index] - data[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for index in range(period + 1, len(data)):
        change = data[index] - data[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[index] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 1e-12 and avg_gain <= 1e-12:
        return 50.0
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def kdj(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 9,
    k_period: int = 3,
    d_period: int = 3,
) -> dict[str, list[Number]]:
    """KDJ：RSV = (C − Ln) / (Hn − Ln) × 100，K/D 用 1/3 权重平滑，J = 3K − 2D。

    国内软件默认 K、D 的平滑参数都是 3（即 2/3 旧值 + 1/3 新值），这里保持一致。
    """
    if period <= 0 or k_period <= 0 or d_period <= 0:
        raise ValueError("周期必须为正整数")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs / lows / closes 长度必须一致")

    high_list = _clean(highs)
    low_list = _clean(lows)
    close_list = _clean(closes)
    size = len(close_list)
    k_out: list[Number] = [None] * size
    d_out: list[Number] = [None] * size
    j_out: list[Number] = [None] * size

    k_prev = 50.0
    d_prev = 50.0
    for index in range(size):
        if index < period - 1:
            continue
        window_high = max(high_list[index - period + 1 : index + 1])
        window_low = min(low_list[index - period + 1 : index + 1])
        span = window_high - window_low
        rsv = 50.0 if span <= 1e-12 else (close_list[index] - window_low) / span * 100.0
        k_value = (k_period - 1) / k_period * k_prev + 1.0 / k_period * rsv
        d_value = (d_period - 1) / d_period * d_prev + 1.0 / d_period * k_value
        k_out[index] = k_value
        d_out[index] = d_value
        j_out[index] = 3.0 * k_value - 2.0 * d_value
        k_prev, d_prev = k_value, d_value

    return {"k": k_out, "d": d_out, "j": j_out}
