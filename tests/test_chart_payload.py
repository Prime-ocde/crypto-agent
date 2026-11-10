"""图表数据整形单测：新的自建组件走 `frontend/series.py` + `frontend/lwc.py`。

锁死的口径：
1. 时间戳必须是 UNIX 秒、严格升序、去重，并平移到本地墙上时间（LWC 按 UTC 渲染）；
2. 脏数据（h < l、非数字）被夹紧/丢弃，不会让组件抛 TimeValidationError；
3. payload 结构（蜡烛/成交量/价格线/标记/副图编号）与 JS 端约定一致。
"""

from __future__ import annotations

import pytest

from frontend.lwc import build_lines_payload, build_payload
from frontend.series import TZ_OFFSET_S, chart_time, clean_rows

MS = 1_700_000_000_000


def candle(offset: int = 0, o=100.0, h=105.0, l=95.0, c=102.0, v=10.0, ts=None) -> dict:
    return {"t": (MS if ts is None else ts) + offset, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_clean_rows_converts_ms_to_seconds() -> None:
    assert clean_rows([candle()])[0]["t"] == MS // 1000


def test_clean_rows_dedupes_keeping_last() -> None:
    rows = clean_rows([candle(), candle(c=111.0)])
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
    row = clean_rows([candle(o=100, h=90, l=110, c=95)])[0]
    assert row["h"] == 110.0
    assert row["l"] == 90.0


def test_chart_time_shifts_to_local_wall_clock() -> None:
    assert chart_time(MS) == MS // 1000 + TZ_OFFSET_S
    assert chart_time(None) is None
    assert chart_time(0) is None


def test_clean_rows_is_idempotent() -> None:
    """二次清洗不能再次平移时间（否则每 rerun 时间轴都会往后跑）。"""
    once = clean_rows([candle()])
    twice = clean_rows(once)
    assert twice[0]["t"] == once[0]["t"]


def test_build_payload_candles_volume_and_price_line() -> None:
    rows = [candle(), candle(offset=60_000, c=99.0)]
    payload = build_payload(rows, price_line={"price": 99.0, "title": "最新"})

    assert len(payload["candles"]) == 2
    assert payload["candles"][0]["open"] == 100.0
    assert payload["candles"][0]["time"] == chart_time(MS)
    assert len(payload["volume"]) == 2
    assert payload["volume"][0]["color"].startswith("rgba(38,166,154")
    assert payload["volume"][1]["color"].startswith("rgba(239,83,80")
    assert payload["priceLine"]["price"] == 99.0
    assert payload["priceLine"]["title"] == "最新"


def test_build_payload_markers_flag_liquidation() -> None:
    payload = build_payload(
        [candle()],
        markers=[
            {"t": MS, "price": 100.0, "side": "buy", "reason": ""},
            {"t": MS + 60_000, "price": 90.0, "side": "sell", "reason": "强平（10x 多）"},
        ],
    )
    markers = payload["markers"]
    assert markers[0]["position"] == "belowBar"
    assert markers[0]["shape"] == "arrowUp"
    assert markers[1]["position"] == "aboveBar"
    assert markers[1]["color"] == "#ef5350"


def test_build_payload_remaps_pane_indices() -> None:
    """内部编号 2/3（0=K线、1=成交量）→ 组件里变成 1/2（成交量是叠加，不占 pane）。"""
    rows = [candle()]
    panes = [
        {"pane": 2, "hist": {"points": [[MS, 1.0]], "colors": ["#26a69a"]},
         "lines": [{"name": "DIF", "color": "#ffb74d", "points": [[MS, 2.0]]}]},
        {"pane": 3, "lines": [{"name": "RSI", "color": "#ba68c8", "points": [[MS, 55.0]]}]},
    ]
    payload = build_payload(rows, panes=panes)
    assert [pane["index"] for pane in payload["panes"]] == [1, 2]
    assert len(payload["panes"][0]["lines"]) == 1


def test_build_payload_skips_empty_panes() -> None:
    payload = build_payload([candle()], panes=[{"pane": 2, "lines": []}])
    assert payload["panes"] == []


def test_build_lines_payload_for_equity_like_charts() -> None:
    series = [
        {"key": "净值", "color": "#4fc3f7", "points": [[MS, 10000.0], [MS + 60_000, 10100.0]]},
        {
            "key": "回撤",
            "color": "#ef5350",
            "type": "area",
            "pane": 1,
            "points": [[MS, 0.0], [MS + 60_000, -1.5]],
        },
    ]
    payload = build_lines_payload(series, reference_lines=[{"price": 0, "color": "#999", "title": "0"}])

    assert payload["candles"] == []
    assert len(payload["lineSeries"]) == 2
    assert payload["lineSeries"][0]["points"][0]["time"] == chart_time(MS)
    assert payload["lineSeries"][1]["type"] == "area"
    assert payload["lineSeries"][1]["pane"] == 1
    assert payload["refLines"][0]["price"] == 0.0


def test_build_lines_payload_drops_empty_series() -> None:
    payload = build_lines_payload([{"key": "空", "points": []}])
    assert payload["lineSeries"] == []
