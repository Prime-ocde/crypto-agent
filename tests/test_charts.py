"""图表适配层单测：只测纯函数，不启动 Streamlit 运行时。

覆盖三个最容易出错的口径：
1. 毫秒 → UNIX 秒（传错会让时间轴错到几万年以后）；
2. 去重 + 升序（LWC 对重复/乱序时间直接抛 TimeValidationError）；
3. 脏数据夹紧（h < l 会让组件校验失败）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit_lightweight_charts_pro")

from frontend.charts import (  # noqa: E402
    clean_rows,
    near_levels,
    range_presets,
    to_candles,
    to_markers,
    to_volume,
)

MS = 1_700_000_000_000


def candle(offset: int = 0, o=100.0, h=105.0, l=95.0, c=102.0, v=10.0, ts=None) -> dict:
    return {"t": (MS if ts is None else ts) + offset, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_clean_rows_converts_ms_to_seconds() -> None:
    rows = clean_rows([candle()])
    assert rows[0]["t"] == MS // 1000


def test_clean_rows_accepts_seconds_and_dedupes_keeping_last() -> None:
    first = candle(ts=1_700_000_000)
    updated = candle(ts=1_700_000_000, c=111.0)
    rows = clean_rows([first, updated])
    assert len(rows) == 1
    assert rows[0]["c"] == 111.0


def test_clean_rows_sorts_and_drops_invalid() -> None:
    rows = clean_rows(
        [
            candle(offset=60_000),
            {"t": None, "o": 1, "h": 1, "l": 1, "c": 1},
            candle(offset=0),
            {"t": MS + 120_000, "o": "x", "h": 1, "l": 1, "c": 1},
        ]
    )
    assert [row["t"] for row in rows] == sorted(row["t"] for row in rows)
    assert len(rows) == 2


def test_clean_rows_clamps_broken_high_low() -> None:
    rows = clean_rows([candle(o=100, h=90, l=110, c=95)])
    assert rows[0]["h"] == 110.0
    assert rows[0]["l"] == 90.0


def test_candles_and_volume_are_built_in_seconds() -> None:
    rows = clean_rows([candle(), candle(offset=60_000, c=99.0)])
    candles = to_candles(rows)
    volumes = to_volume(rows)
    assert candles[0].time == MS // 1000
    assert candles[0].open == 100.0
    assert volumes[0].color.startswith("rgba(38,166,154")
    assert volumes[1].color.startswith("rgba(239,83,80")


def test_markers_flag_liquidation_and_direction() -> None:
    markers = to_markers(
        [
            {"t": MS, "price": 100.0, "side": "buy", "reason": ""},
            {"t": MS + 60_000, "price": 90.0, "side": "sell", "reason": "强平（10x 多）"},
        ]
    )
    assert len(markers) == 2
    assert markers[0].position == "belowBar"
    assert markers[1].position == "aboveBar"
    assert markers[1].color == "#ef5350"


def test_markers_skip_bad_records() -> None:
    assert to_markers([{"t": None, "price": 1, "side": "buy"}, {"t": MS, "price": None}]) == []


def test_near_levels_filters_far_grid_lines() -> None:
    rows = clean_rows([candle(o=100, h=110, l=90, c=100)])
    levels = [50.0, 99.0, 101.0, 500.0]
    assert near_levels(rows, levels) == [99.0, 101.0]
    assert near_levels(rows, None) == []
    assert near_levels([], levels) == []


def test_range_presets_are_interval_specific_and_end_with_all() -> None:
    daily = range_presets("1d")
    assert [item.text for item in daily][-1] == "全部"
    assert "15m" not in [item.text for item in daily]
    assert "15m" in [item.text for item in range_presets("1m")]
    assert range_presets("unknown")  # 未知周期回落到日线预设，不抛异常
