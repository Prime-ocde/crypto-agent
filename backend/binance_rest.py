"""Binance 公共 REST 行情：启动预热 + 离线 CSV 缓存兜底。

不用 API Key：只读 `/api/v3/klines`。阶段二起主域名统一为
`data-api.binance.vision`（币安官方"仅公共行情"域名，通常更通），
仅在它不可达时才降级到 `api.binance.com`。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import requests

from backend.config import DATA_DIR

logger = logging.getLogger("grid.backend.rest")

_HEADERS = {"User-Agent": "crypto-grid-quant/0.1 (course project; paper trading)"}
_TIMEOUT = (3.0, 8.0)


def _cache_path(symbol: str, interval: str) -> Path:
    return DATA_DIR / f"cache_{symbol.upper()}_{interval}.csv"


def _row_to_candle(row: list) -> dict:
    return {
        "t": int(row[0]),
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]),
        "closed": True,
    }


def fetch_klines(
    base_urls: tuple[str, ...] | list[str],
    symbol: str,
    interval: str,
    limit: int = 500,
) -> tuple[list[dict], str | None]:
    """拉历史 K 线。

    Returns:
        (candles, base_url)：全部域名都失败时返回 `([], None)`，由上层降级到缓存/合成。
    """
    limit = max(1, min(int(limit), 1000))
    last_error: str | None = None
    for base in base_urls:
        url = f"{base.rstrip('/')}/api/v3/klines"
        try:
            response = requests.get(
                url,
                params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            candles = [_row_to_candle(row) for row in payload]
            if candles:
                logger.info("REST 预热成功：%s %s %s 根（来源 %s）", symbol, interval, len(candles), base)
                return candles, base
            last_error = f"{base} 返回空数据"
        except Exception as exc:  # noqa: BLE001 - 网络异常种类太多，统一降级
            last_error = f"{base}: {exc}"
            logger.warning("REST 预热失败 %s %s @ %s: %s", symbol, interval, base, exc)
    if last_error:
        logger.warning("REST 预热全部失败：%s", last_error)
    return [], None


def save_cache(symbol: str, interval: str, candles: list[dict]) -> Path:
    """把历史 K 线落盘，供断网时前端/后端继续演示。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, interval)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "o", "h", "l", "c", "v"])
        for candle in candles:
            writer.writerow([candle["t"], candle["o"], candle["h"], candle["l"], candle["c"], candle["v"]])
    return path


def load_cache(symbol: str, interval: str) -> list[dict]:
    path = _cache_path(symbol, interval)
    if not path.exists():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            candles = [
                {
                    "t": int(float(row["t"])),
                    "o": float(row["o"]),
                    "h": float(row["h"]),
                    "l": float(row["l"]),
                    "c": float(row["c"]),
                    "v": float(row.get("v") or 0.0),
                    "closed": True,
                }
                for row in reader
            ]
    except (OSError, KeyError, ValueError) as exc:
        logger.warning("读取本地缓存失败 %s: %s", path, exc)
        return []
    candles.sort(key=lambda candle: candle["t"])
    return candles
