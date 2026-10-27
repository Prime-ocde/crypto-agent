"""K 线周期工具：周期毫秒长度、年化系数、原生/派生周期划分。

币安原生支持到 `1M`（月）；**没有 1y**，所以年 K 由月 K 聚合得到（见 `core/aggregate.py`）。
"""

from __future__ import annotations

from typing import Final

INTERVAL_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,  # 30 天近似，仅用于年化系数
    "1y": 31_536_000_000,  # 365 天近似，仅用于年化系数
}

SUPPORTED_INTERVALS: Final[tuple[str, ...]] = tuple(INTERVAL_MS)

# 币安 REST / WebSocket 原生支持的周期（与 INTERVAL_MS 的交集）
NATIVE_INTERVALS: Final[tuple[str, ...]] = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "1d",
    "1w",
    "1M",
)

# 派生周期 → 基础周期：1y 由 12 根月 K 聚合
DERIVED_INTERVALS: Final[dict[str, str]] = {"1y": "1M"}

SECONDS_PER_YEAR: Final[float] = 365.0 * 24 * 3600

_INTERVAL_LABELS: Final[dict[str, str]] = {
    "1m": "1 分钟",
    "3m": "3 分钟",
    "5m": "5 分钟",
    "15m": "15 分钟",
    "30m": "30 分钟",
    "1h": "1 小时",
    "2h": "2 小时",
    "4h": "4 小时",
    "1d": "1 天",
    "1w": "1 周",
    "1M": "1 月",
    "1y": "1 年",
}


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(
            f"不支持的 K 线周期：{interval!r}，可选：{', '.join(SUPPORTED_INTERVALS)}"
        ) from None


def bars_per_year(interval: str) -> float:
    """一年有多少根该周期的 K 线（用于夏普年化）。"""
    return SECONDS_PER_YEAR * 1000.0 / interval_ms(interval)


def interval_label(interval: str) -> str:
    return _INTERVAL_LABELS.get(interval, interval)


def base_interval(interval: str) -> str:
    """派生周期返回其基础周期，原生周期返回自身。"""
    return DERIVED_INTERVALS.get(interval, interval)


def required_intervals(intervals: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """把派生周期展开成需要真实订阅的基础周期（去重、保序）。"""
    ordered: list[str] = []
    for interval in intervals:
        base = base_interval(interval)
        if base not in ordered:
            ordered.append(base)
    return tuple(ordered)
