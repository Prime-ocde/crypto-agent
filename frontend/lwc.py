"""自建 Lightweight Charts 组件：增量更新、保留缩放、OHLC 悬浮框。

为什么不继续用 `streamlit-lightweight-charts-pro`：
它的 JS 只在首次挂载或 `forceReinit` 时建图，没有 `series.update()` 通道，
所以每次刷新价格都要销毁重建 → 用户缩放/平移会被重置、偶发白屏。

这里直接内嵌官方 `lightweight-charts` standalone（已 vendor 到 `frontend/lwc_component/`，
不依赖任何 CDN），组件实例常驻：价格变化只 `update()` 最后一根，视口完全不动。

时间戳沿用 `frontend.charts` 的约定：已平移到「本地墙上时间」，JS 侧用 UTC getter 格式化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import streamlit.components.v1 as components

from frontend.series import chart_time as _chart_time
from frontend.series import clean_rows, finite as _finite

_COMPONENT_DIR = Path(__file__).parent / "lwc_component"
_component = components.declare_component("lwc_min", path=str(_COMPONENT_DIR))


def build_payload(
    candles: Iterable[dict] | None,
    *,
    height: int = 520,
    overlays: Iterable[dict] | None = None,
    price_line: dict | None = None,
    price_lines: Iterable[dict] | None = None,
    markers: Iterable[dict] | None = None,
    panes: Iterable[dict] | None = None,
    reset_view: bool = False,
) -> dict:
    """把内部 K 线/指标/成交记录翻译成组件要的 JSON。"""
    rows = clean_rows(candles)
    candle_points = [
        {
            "time": _chart_time(row["t"]),
            "open": row["o"],
            "high": row["h"],
            "low": row["l"],
            "close": row["c"],
        }
        for row in rows
    ]
    volume_points = [
        {
            "time": _chart_time(row["t"]),
            "value": row["v"],
            "color": "rgba(38,166,154,0.40)" if row["c"] >= row["o"] else "rgba(239,83,80,0.40)",
        }
        for row in rows
    ]

    overlay_payload: list[dict] = []
    for spec in overlays or []:
        points = []
        for point in spec.get("points") or []:
            ts = _chart_time(point[0])
            value = _finite(point[1])
            if ts is None or value is None:
                continue
            points.append({"time": ts, "value": value})
        if points:
            overlay_payload.append(
                {
                    "key": spec.get("name", ""),
                    "color": spec.get("color", "#4fc3f7"),
                    "width": int(spec.get("width", 1)),
                    "points": points,
                }
            )

    marker_payload: list[dict] = []
    for record in markers or []:
        ts = _chart_time(record.get("t"))
        price = _finite(record.get("price"))
        if ts is None or price is None:
            continue
        side = str(record.get("side") or "")
        reason = str(record.get("reason") or "")
        is_buy = side == "buy"
        color = "#ef5350" if "强平" in reason else ("#26a69a" if is_buy else "#ef5350")
        marker_payload.append(
            {
                "time": ts,
                "position": "belowBar" if is_buy else "aboveBar",
                "color": color,
                "shape": "arrowUp" if is_buy else "arrowDown",
                "text": f"{'买' if is_buy else '卖'} {price:,.2f}",
            }
        )

    # 指标副图：内部 pane 编号从 2 开始（0=K线、1=成交量），组件里成交量是叠加在 pane 0 的，
    # 所以指标从 pane 1 起排。
    pane_payload: list[dict] = []
    for pane in panes or []:
        index = max(1, int(pane.get("pane", 2)) - 1)
        entry: dict[str, Any] = {"index": index, "lines": []}
        hist = pane.get("hist")
        if hist:
            points = []
            for point in hist.get("points") or []:
                ts = _chart_time(point[0])
                value = _finite(point[1])
                if ts is None or value is None:
                    continue
                points.append({"time": ts, "value": value, "color": point[2] if len(point) > 2 else None})
            colors = list(hist.get("colors") or [])
            for i, point in enumerate(points):
                if not point.get("color") and i < len(colors):
                    point["color"] = colors[i]
            if points:
                entry["hist"] = {"points": points}
        for line in pane.get("lines") or []:
            points = []
            for point in line.get("points") or []:
                ts = _chart_time(point[0])
                value = _finite(point[1])
                if ts is None or value is None:
                    continue
                points.append({"time": ts, "value": value})
            if points:
                entry["lines"].append(
                    {
                        "key": line.get("name", ""),
                        "color": line.get("color", "#ffb74d"),
                        "width": int(line.get("width", 1)),
                        "points": points,
                    }
                )
        if entry.get("hist") or entry["lines"]:
            pane_payload.append(entry)

    payload: dict[str, Any] = {
        "height": int(height),
        "resetView": bool(reset_view),
        "candles": candle_points,
        "volume": volume_points,
        "overlays": overlay_payload,
        "markers": marker_payload,
        "panes": pane_payload,
    }
    if price_line:
        payload["priceLine"] = {
            "price": float(price_line.get("price") or 0.0),
            "color": price_line.get("color", "#ffb74d"),
            "title": price_line.get("title", "最新"),
        }
    extra_lines = []
    for spec in price_lines or []:
        try:
            price = float(spec.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        extra_lines.append(
            {
                "price": price,
                "color": spec.get("color", "rgba(79,195,247,0.45)"),
                "title": spec.get("title", ""),
            }
        )
    if extra_lines:
        payload["priceLines"] = extra_lines
    return payload


def render(payload: dict, key: str, height: int | None = None) -> Any:
    """渲染组件；`key` 必须稳定，否则会重建 iframe（但图表本身不会被重建）。"""
    return _component(config=payload, height=height or payload.get("height", 520), key=key, default=None)


def build_lines_payload(
    series: Iterable[dict],
    *,
    height: int = 420,
    reset_view: bool = False,
    reference_lines: Iterable[dict] | None = None,
) -> dict:
    """纯折线/面积图（净值+回撤、策略对比、情绪曲线）。

    `series` 每项：`{"key","color","width","pane","type":"line|area","fill","fill_bottom","points":[[ms, value]]}`。
    """
    line_payload: list[dict] = []
    for spec in series or []:
        points = []
        for point in spec.get("points") or []:
            ts = _chart_time(point[0])
            value = _finite(point[1])
            if ts is None or value is None:
                continue
            points.append({"time": ts, "value": value})
        if not points:
            continue
        line_payload.append(
            {
                "key": spec.get("key") or spec.get("name", ""),
                "color": spec.get("color", "#4fc3f7"),
                "width": int(spec.get("width", 2)),
                "pane": int(spec.get("pane", 0)),
                "type": spec.get("type", "line"),
                "fill": spec.get("fill"),
                "fillBottom": spec.get("fill_bottom"),
                "points": points,
            }
        )

    ref_payload = []
    for spec in reference_lines or []:
        try:
            price = float(spec.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        ref_payload.append(
            {
                "price": price,
                "color": spec.get("color", "rgba(0,0,0,0.35)"),
                "title": spec.get("title", ""),
            }
        )

    return {
        "height": int(height),
        "resetView": bool(reset_view),
        "candles": [],
        "lineSeries": line_payload,
        "refLines": ref_payload,
    }
