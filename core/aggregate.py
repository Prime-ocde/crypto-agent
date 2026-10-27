"""K 线聚合：把细粒度 K 线聚合成周 / 月 / 年 K。

为什么需要它：
- 币安原生支持 `1w` / `1M`，但**没有 1y**，年 K 只能由 12 根月 K 聚合；
- 周 K 必须按 ISO 周（周一 00:00 UTC）切桶，月 K 按自然月，年 K 按自然年 ——
  不能用固定毫秒数整除，否则跨年跨月的桶会错位。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

_CALENDAR_INTERVALS = {"1w", "1M", "1y"}


def bucket_start(ts_ms: int, interval: str) -> int:
    """返回该时间戳所属周期的起始毫秒（UTC 日历对齐）。"""
    if interval == "1w":
        moment = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        start = moment - timedelta(
            days=moment.weekday(),
            hours=moment.hour,
            minutes=moment.minute,
            seconds=moment.second,
            microseconds=moment.microsecond,
        )
        return int(start.timestamp() * 1000)
    if interval == "1M":
        moment = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        start = datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)
        return int(start.timestamp() * 1000)
    if interval == "1y":
        moment = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        start = datetime(moment.year, 1, 1, tzinfo=timezone.utc)
        return int(start.timestamp() * 1000)
    raise ValueError(f"不支持的聚合周期：{interval!r}（仅支持 1w / 1M / 1y）")


def aggregate_candles(candles: Iterable[dict], target_interval: str) -> list[dict]:
    """把任意细粒度 K 线聚合到 `target_interval`。

    聚合规则：open 取第一根、high 取最大、low 取最小、close 取最后一根、volume 求和。
    `closed` 取该桶内最后一根的状态（最后一桶通常是进行中的 K 线）。
    """
    if target_interval not in _CALENDAR_INTERVALS:
        raise ValueError(f"不支持的聚合周期：{target_interval!r}")

    ordered = sorted((candle for candle in candles if candle), key=lambda candle: candle["t"])
    buckets: dict[int, dict] = {}
    sequence: list[int] = []

    for candle in ordered:
        start = bucket_start(int(candle["t"]), target_interval)
        current = buckets.get(start)
        if current is None:
            buckets[start] = {
                "t": start,
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
                "v": float(candle.get("v") or 0.0),
                "closed": bool(candle.get("closed", True)),
            }
            sequence.append(start)
            continue
        current["h"] = max(current["h"], float(candle["h"]))
        current["l"] = min(current["l"], float(candle["l"]))
        current["c"] = float(candle["c"])
        current["v"] += float(candle.get("v") or 0.0)
        current["closed"] = bool(candle.get("closed", True))

    return [buckets[start] for start in sequence]
