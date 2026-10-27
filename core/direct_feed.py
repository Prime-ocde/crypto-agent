"""Binance 公共 REST 直连：给"没有后端"的云端部署兜底行情。

前端（Streamlit Cloud）在拿不到 FastAPI 后端时，可以由**服务端进程**直接请求
`data-api.binance.vision` 的 klines 接口，避免浏览器跨域问题，也避免依赖自建后端。

- 1y 没有原生接口：先用 1M 拉 12 倍数量，再用 `core.aggregate` 聚合；
- 任何异常都返回空列表，由上层继续降级到缓存/合成行情。
"""

from __future__ import annotations

from typing import Any

import requests

from core.aggregate import aggregate_candles
from core.timeframe import DERIVED_INTERVALS

BINANCE_REST_BASES: tuple[str, ...] = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)

_DERIVED_RATIO = {"1y": 12}
_TIMEOUT = (4.0, 8.0)


def parse_klines(rows: Any) -> list[dict]:
    """把 Binance klines 数组转成内部 K 线结构。"""
    candles: list[dict] = []
    for row in rows or []:
        try:
            candles.append(
                {
                    "t": int(row[0]),
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                    "closed": True,
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return candles


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 300,
    timeout: tuple[float, float] = _TIMEOUT,
) -> list[dict]:
    """直连 Binance 公共 REST 拉 K 线；失败返回空列表。"""
    fetch_interval = interval
    fetch_limit = limit
    if interval in DERIVED_INTERVALS:
        fetch_interval = DERIVED_INTERVALS[interval]
        ratio = _DERIVED_RATIO.get(interval, 12)
        fetch_limit = min(1000, limit * ratio + ratio)

    for base in BINANCE_REST_BASES:
        try:
            response = requests.get(
                f"{base.rstrip('/')}/api/v3/klines",
                params={"symbol": symbol.upper(), "interval": fetch_interval, "limit": fetch_limit},
                timeout=timeout,
                headers={"User-Agent": "crypto-agent/0.1 (course project; read-only)"},
            )
            response.raise_for_status()
            candles = parse_klines(response.json())
            if not candles:
                continue
            if interval in DERIVED_INTERVALS:
                candles = aggregate_candles(candles, interval)[-limit:]
            return candles
        except Exception:  # noqa: BLE001 - 网络/解析失败统一降级
            continue
    return []
