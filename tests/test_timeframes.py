"""周期与聚合单测：周/月/年 K 的桶对齐、聚合规则与年化系数。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.aggregate import aggregate_candles, bucket_start
from core.timeframe import (
    SUPPORTED_INTERVALS,
    bars_per_year,
    base_interval,
    interval_ms,
    required_intervals,
)


def _ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def test_new_intervals_registered() -> None:
    for interval in ("1w", "1M", "1y"):
        assert interval in SUPPORTED_INTERVALS
    assert interval_ms("1w") > interval_ms("1d")
    assert interval_ms("1M") > interval_ms("1w")
    assert interval_ms("1y") > interval_ms("1M")


def test_required_intervals_expands_derived() -> None:
    assert required_intervals(("1m", "1y")) == ("1m", "1M")
    assert required_intervals(("1y", "1M")) == ("1M",)
    assert base_interval("1y") == "1M"
    assert base_interval("1w") == "1w"


def test_week_bucket_starts_on_monday_utc() -> None:
    # 2026-10-27 是周二
    start = bucket_start(_ms(2026, 10, 27, 13), "1w")
    moment = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
    assert moment.weekday() == 0
    assert (moment.hour, moment.minute, moment.second) == (0, 0, 0)


def test_month_bucket_starts_on_first_day() -> None:
    start = bucket_start(_ms(2026, 10, 27), "1M")
    assert start == _ms(2026, 10, 1)


def test_year_bucket_starts_on_january_first() -> None:
    start = bucket_start(_ms(2026, 10, 27), "1y")
    assert start == _ms(2026, 1, 1)


def test_aggregate_daily_into_month() -> None:
    candles = [
        {"t": _ms(2026, 1, day), "o": 100 + day, "h": 105 + day, "l": 95 + day, "c": 100 + day, "v": 1.0}
        for day in range(1, 32)
    ]
    monthly = aggregate_candles(candles, "1M")

    assert len(monthly) == 1
    assert monthly[0]["o"] == 101
    assert monthly[0]["c"] == 131
    assert monthly[0]["h"] == 136
    assert monthly[0]["l"] == 96
    assert monthly[0]["v"] == pytest.approx(31.0)
    assert monthly[0]["t"] == _ms(2026, 1, 1)


def test_aggregate_monthly_into_year() -> None:
    candles = [
        {"t": _ms(year, month, 1), "o": 100.0, "h": 110.0, "l": 90.0, "c": 105.0, "v": 10.0}
        for year in (2025, 2026)
        for month in range(1, 13)
    ]
    yearly = aggregate_candles(candles, "1y")

    assert [candle["t"] for candle in yearly] == [_ms(2025, 1, 1), _ms(2026, 1, 1)]
    assert yearly[0]["v"] == pytest.approx(120.0)
    assert all(candle["o"] == 100.0 and candle["c"] == 105.0 for candle in yearly)


def test_aggregate_rejects_unknown_interval() -> None:
    with pytest.raises(ValueError, match="不支持的聚合周期"):
        aggregate_candles([], "1h")


def test_bars_per_year_for_long_intervals() -> None:
    assert bars_per_year("1w") == pytest.approx(52.14, rel=1e-3)
    assert bars_per_year("1M") == pytest.approx(12.17, rel=1e-3)
    assert bars_per_year("1y") == pytest.approx(1.0)
