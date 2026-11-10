"""K 线数据整形（纯函数，不依赖任何图表库）。

从旧的 `frontend/charts.py` 抽出来：组件换掉之后这些口径不能变——
时间戳必须是严格升序、去重、且平移到本地墙上时间（Lightweight Charts 按 UTC 渲染）。
"""

from __future__ import annotations

import math
import time
from typing import Any, Iterable, Sequence


def _seconds(ms: Any) -> int | None:
    """毫秒时间戳 → UNIX 秒；非法值返回 None。"""
    try:
        value = int(float(ms))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value // 1000 if value > 10_000_000_000 else value


def _local_offset_seconds() -> int:
    try:
        return int(time.localtime().tm_gmtoff)
    except (AttributeError, OSError):  # pragma: no cover - 极端平台兜底
        return 0


TZ_OFFSET_S = _local_offset_seconds()


def chart_time(ms: Any) -> int | None:
    """毫秒时间戳 → 本地墙上时间的 UNIX 秒（直接喂给图表）。"""
    seconds = _seconds(ms)
    return None if seconds is None else seconds + TZ_OFFSET_S


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_rows(candles: Iterable[dict] | None) -> list[dict]:
    """规范化 + 去重 + 升序（图表对重复/乱序时间直接抛错）。"""
    by_time: dict[int, dict] = {}
    for candle in candles or []:
        ts = _seconds(candle.get("t"))
        if ts is None:
            continue
        o, h, l, c = (finite(candle.get(k)) for k in ("o", "h", "l", "c"))
        if None in (o, h, l, c):
            continue
        row = {
            "t": ts,
            "o": o,
            "h": max(o, h, l, c),
            "l": min(o, h, l, c),
            "c": c,
            "v": finite(candle.get("v")) or 0.0,
        }
        by_time[ts] = row
    return [by_time[ts] for ts in sorted(by_time)]


def points(series: Sequence[float | None], times: Sequence[int]) -> list[list[float]]:
    """把「与 K 线等长的指标序列」转成 `[[ms, value], ...]`（跳过预热期的 None）。"""
    return [[ts, value] for ts, value in zip(times, series) if value is not None]
