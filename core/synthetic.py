"""离线行情合成器（Mock 数据源）。

两个用途：
1. 后端 `RUN_MODE=mock` 时，完全不联网也能持续推 K 线；
2. 前端既连不上后端、本地又没有缓存 CSV 时的最后一道兜底。

生成模型：带均值回归的几何随机游走 + 随机影线 + 随机成交量。
形态接近真实行情，但**不是真实历史数据**，界面上会明确标注。
"""

from __future__ import annotations

import random
import time
import zlib

from core.timeframe import interval_ms

# 各品种的"心理价位"，仅用于让 Mock 行情看起来不离谱（数值会随市场漂移，不必较真）
BASE_PRICE_BY_SYMBOL: dict[str, float] = {
    "BTCUSDT": 79_800.0,
    "ETHUSDT": 2_315.0,
    "BNBUSDT": 738.0,
    "SOLUSDT": 100.0,
    "XRPUSDT": 2.4,
    "DOGEUSDT": 0.24,
}

VOLATILITY_BY_INTERVAL: dict[str, float] = {
    "1m": 0.0012,
    "3m": 0.0020,
    "5m": 0.0026,
    "15m": 0.0045,
    "30m": 0.0065,
    "1h": 0.0090,
    "2h": 0.0130,
    "4h": 0.0180,
    "1d": 0.0300,
}

MEAN_REVERSION = 0.02
_MIN_PRICE = 1e-8


def default_base_price(symbol: str) -> float:
    """给一个稳定、可复现的基准价（未知品种也不会崩）。"""
    upper = symbol.upper()
    for key, price in BASE_PRICE_BY_SYMBOL.items():
        if upper == key:
            return price
    for key, price in BASE_PRICE_BY_SYMBOL.items():
        if upper.startswith(key[:-4]):  # 去掉 USDT 后缀做前缀匹配
            return price
    digest = zlib.crc32(upper.encode("utf-8"))
    return 10.0 + float(digest % 90_000) / 100.0


def synthesize_candles(
    symbol: str,
    interval: str = "1m",
    limit: int = 300,
    end_ts_ms: int | None = None,
    base_price: float | None = None,
    volatility: float | None = None,
    seed: int | None = None,
) -> list[dict]:
    """生成 `limit` 根首尾相接的 K 线，最后一根视为"正在形成中"。"""
    if limit <= 0:
        raise ValueError("limit 必须为正整数")

    step = interval_ms(interval)
    vol = float(volatility) if volatility is not None else VOLATILITY_BY_INTERVAL.get(interval, 0.002)
    anchor = float(base_price) if base_price is not None else default_base_price(symbol)

    end = int(end_ts_ms) if end_ts_ms is not None else int(time.time() * 1000)
    end -= end % step  # 对齐到周期边界
    start_ts = end - step * (limit - 1)

    digest = zlib.crc32(f"{symbol.upper()}:{interval}".encode("utf-8"))
    rng = random.Random(seed if seed is not None else digest)

    candles: list[dict] = []
    price = anchor
    prev_close = anchor
    for i in range(limit):
        drift = (anchor - price) / anchor * MEAN_REVERSION
        shock = rng.gauss(0.0, vol) + drift
        close = max(price * (1.0 + shock), _MIN_PRICE)

        open_ = prev_close
        wick = abs(rng.gauss(0.0, vol * 0.6))
        high = max(open_, close) * (1.0 + wick)
        low = min(open_, close) * (1.0 - wick)
        volume = abs(rng.gauss(1.0, 0.35)) * (anchor ** 0.5) * 12.0

        candles.append(
            {
                "t": start_ts + i * step,
                "o": round(open_, 6),
                "h": round(max(high, open_, close), 6),
                "l": round(min(low, open_, close), 6),
                "c": round(close, 6),
                "v": round(volume, 6),
                "closed": i < limit - 1,
            }
        )
        prev_close = close
        price = close

    return candles
