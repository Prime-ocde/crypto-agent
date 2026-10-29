"""K 线图表适配层：TradingView Lightweight Charts（streamlit-lightweight-charts-pro 0.3.0）。

## 为什么单独一层

`frontend/app.py` 里原本有 6 处 `plotly_chart(...)`。把渲染细节全部塞进 app.py 会让
那个 1500 行的文件更难维护，也无法单测。这里只做三件事：

1. **数据整形**：内部 K 线是 `{"t": ms, "o","h","l","c","v"}`，LWC 要的是
   UNIX **秒**、严格升序、无重复的 dataclass；转换与清洗全部集中在这里；
2. **视觉映射**：暗色主题、右轴、蜡烛 + 成交量双 pane、网格线、最新价线、均线；
3. **渲染**：`Chart.render(key=...)`，key 必须稳定，否则组件每次 rerun 都会重建、缩放丢失。

## 与旧 Plotly 版本的差异（有意为之）

- Plotly 用 `update_yaxes(range=...)` 强制纵轴范围把远处网格线裁掉；LWC 没有等价的
  `setPriceRange`，所以这里改成**只画视野附近的网格线**（同样的 `band` 公式），
  效果一致但不再需要命令式设置范围；
- Plotly 的 `rangeselector`（含 `step="week"` 那个补丁）换成 LWC 原生 range switcher，
  按钮按周期映射到 `TimeRange` 枚举；
- Plotly 的 `uirevision` 由 LWC 的**稳定 key + 组件内部状态**承担。

## 口径

时间戳去重时保留**后出现**的一条（最后一根未收盘 K 线会不断被覆盖更新）。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from lightweight_charts_pro.charts.options import LineOptions, PriceLineOptions
from lightweight_charts_pro.charts.options.ui_options import (
    RangeConfig,
    RangeSwitcherOptions,
    TimeRange,
)
from streamlit_lightweight_charts_pro import (
    CandlestickData,
    CandlestickSeries,
    Chart,
    ChartOptions,
    HistogramData,
    HistogramSeries,
    LineData,
    LineSeries,
    LineStyle,
    Marker,
    MarkerPosition,
)

# 与旧 Plotly 版本保持一致的配色
GREEN = "#26a69a"
RED = "#ef5350"
AMBER = "#ffb74d"
BLUE = "#4fc3f7"
GREY = "#9e9e9e"

BACKGROUND = "#ffffff"
TEXT_COLOR = "#131722"
GRID_COLOR = "rgba(0,0,0,0.06)"
BORDER_COLOR = "rgba(0,0,0,0.12)"

VOLUME_SCALE_ID = "volume"


class StableChart(Chart):
    """把 `chartId` 固定住。

    库的 `to_frontend_config()` 用的是 `chart-{id(self)}`（内存地址），每次 rerun 都不同。
    组件收到"新 id"就会重建整张图：缩放被重置、偶发白屏（初始化被打断）。
    这里按传入的 key 派生一个稳定 id，让 2 秒刷新只更新数据、不重建图表。
    """

    def __init__(self, *args: Any, chart_id: str = "chart", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stable_chart_id = chart_id

    def to_frontend_config(self) -> dict:
        config = super().to_frontend_config()
        for item in config.get("charts") or []:
            item["chartId"] = self._stable_chart_id
        return config


def _seconds(ms: Any) -> int | None:
    """毫秒时间戳 → UNIX 秒；非法值返回 None。"""
    try:
        value = int(float(ms))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value // 1000 if value > 10_000_000_000 else value


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_rows(candles: Iterable[dict] | None) -> list[dict]:
    """规范化 + 去重 + 升序。LWC 对重复/乱序时间直接抛 TimeValidationError。"""
    by_time: dict[int, dict] = {}
    for candle in candles or []:
        ts = _seconds(candle.get("t"))
        if ts is None:
            continue
        o, h, l, c = (_finite(candle.get(k)) for k in ("o", "h", "l", "c"))
        if None in (o, h, l, c):
            continue
        # 脏数据（h < l）会让组件校验失败，这里直接夹紧
        row = {
            "t": ts,
            "o": o,
            "h": max(o, h, l, c),
            "l": min(o, h, l, c),
            "c": c,
            "v": _finite(candle.get("v")) or 0.0,
        }
        by_time[ts] = row
    return [by_time[ts] for ts in sorted(by_time)]


def to_candles(rows: Sequence[dict]) -> list[CandlestickData]:
    return [
        CandlestickData(
            time=row["t"],
            open=row["o"],
            high=row["h"],
            low=row["l"],
            close=row["c"],
        )
        for row in rows
    ]


def to_volume(rows: Sequence[dict]) -> list[HistogramData]:
    """成交量按当根涨跌染色（LWC 的 histogram 支持逐点 color）。"""
    return [
        HistogramData(
            time=row["t"],
            value=row["v"],
            color="rgba(38,166,154,0.40)" if row["c"] >= row["o"] else "rgba(239,83,80,0.40)",
        )
        for row in rows
    ]


def to_line(points: Iterable[Sequence[float]]) -> list[LineData]:
    """`[[ms, price], ...]` → LWC 折线点（均线、净值曲线都走这里）。"""
    out: list[LineData] = []
    for point in points or []:
        try:
            ts = _seconds(point[0])
            value = _finite(point[1])
        except (TypeError, IndexError):
            continue
        if ts is None or value is None:
            continue
        out.append(LineData(time=ts, value=value))
    return out


def to_markers(records: Iterable[dict] | None) -> list[Marker]:
    """账本成交记录 → K 线上的买卖标记。

    口径与 `core/execution.py` 的录音一致：`side=buy` 是买入/平空，`sell` 是卖出/平多；
    强平记录额外标红，方便在图上直接看到爆仓点。
    """
    markers: list[Marker] = []
    for record in records or []:
        ts = _seconds(record.get("t"))
        price = _finite(record.get("price"))
        if ts is None or price is None:
            continue
        side = str(record.get("side") or "")
        reason = str(record.get("reason") or "")
        is_buy = side == "buy"
        position = MarkerPosition.BELOW_BAR if is_buy else MarkerPosition.ABOVE_BAR
        if "强平" in reason:
            color = RED
            text = f"强平 {price:,.2f}"
        elif is_buy:
            color = GREEN
            text = f"买 {price:,.2f}"
        else:
            color = RED
            text = f"卖 {price:,.2f}"
        markers.append(
            Marker(
                time=ts,
                position=position,
                shape="arrowUp" if is_buy else "arrowDown",
                color=color,
                text=text,
                price=price,
                size=1,
            )
        )
    return markers


def near_levels(rows: Sequence[dict], levels: Iterable[float] | None, ratio: float = 0.4) -> list[float]:
    """只保留价格视野附近的参考线（替代 Plotly 的 update_yaxes(range=...) 裁剪）。"""
    if not rows or not levels:
        return []
    low = min(row["l"] for row in rows)
    high = max(row["h"] for row in rows)
    last = rows[-1]["c"]
    band = (high - low) * ratio or last * 0.002
    return [float(level) for level in levels if low - band <= float(level) <= high + band]


def range_presets(interval: str) -> list[RangeConfig]:
    """周期化区间按钮：长周期不该出现「15 分钟」这种按钮。"""
    presets: dict[str, list[tuple[str, TimeRange]]] = {
        "1m": [("15m", TimeRange.FIFTEEN_MINUTES), ("1h", TimeRange.ONE_HOUR), ("4h", TimeRange.FOUR_HOURS)],
        "5m": [("1h", TimeRange.ONE_HOUR), ("4h", TimeRange.FOUR_HOURS), ("1d", TimeRange.ONE_DAY)],
        "1h": [("1d", TimeRange.ONE_DAY), ("1w", TimeRange.ONE_WEEK), ("1M", TimeRange.ONE_MONTH)],
        "1d": [("1w", TimeRange.ONE_WEEK), ("1M", TimeRange.ONE_MONTH), ("3M", TimeRange.THREE_MONTHS)],
        "1w": [("3M", TimeRange.THREE_MONTHS), ("1y", TimeRange.ONE_YEAR), ("5y", TimeRange.FIVE_YEARS)],
        "1M": [("1y", TimeRange.ONE_YEAR), ("5y", TimeRange.FIVE_YEARS)],
        "1y": [("5y", TimeRange.FIVE_YEARS)],
    }
    chosen = presets.get(interval, presets["1d"])
    return [RangeConfig(text=label, range=value) for label, value in [*chosen, ("全部", TimeRange.ALL)]]


def build_options(interval: str, height: int = 520) -> ChartOptions:
    options = ChartOptions(height=height)  # auto_size 默认 True：宽度跟随 Streamlit 容器
    # 该版本前端的 layout 只读取 `panes`（源码：applyOptions({layout:{panes}}）），
    # background / paneHeights 都会被忽略，所以这里只设置确实生效的文字与网格颜色。
    options.layout.text_color = TEXT_COLOR
    options.grid.horz_lines.color = GRID_COLOR
    options.grid.vert_lines.visible = False
    options.time_scale.border_color = BORDER_COLOR
    options.time_scale.seconds_visible = False
    options.right_price_scale.border_color = BORDER_COLOR
    options.right_price_scale.text_color = TEXT_COLOR
    options.right_price_scale.scale_margins.top = 0.08
    options.right_price_scale.scale_margins.bottom = 0.08
    options.range_switcher = RangeSwitcherOptions(visible=True, ranges=range_presets(interval))
    return options


def build_chart(
    candles: Iterable[dict] | None,
    levels: Iterable[float] | None = None,
    lines: Iterable[dict] | None = None,
    markers: Iterable[dict] | None = None,
    interval: str = "1m",
    height: int = 520,
    price_lines: Iterable[dict] | None = None,
    chart_id: str = "chart",
    overlay_lines: Iterable[dict] | None = None,
    sub_panes: Iterable[dict] | None = None,
    force_reinit: bool = False,
) -> Chart | None:
    """组装 Chart 对象；数据为空返回 None（由调用方显示空状态）。

    `price_lines` 是手工指定的水平线（强平价、开仓价等），与网格线分开：
    它们永远画、不参与 `near_levels` 的视野裁剪。
    """
    rows = clean_rows(candles)
    if not rows:
        return None

    candle_series = CandlestickSeries(data=to_candles(rows))
    candle_series.up_color = GREEN
    candle_series.down_color = RED
    candle_series.border_visible = False
    candle_series.wick_up_color = GREEN
    candle_series.wick_down_color = RED
    # 最后一价的标签交给「最新价」价格线统一显示，避免同一个价格出现两个标签
    candle_series.last_value_visible = False

    volume_series = HistogramSeries(
        data=to_volume(rows),
        price_scale_id=VOLUME_SCALE_ID,
        pane_id=1,
    )
    # 成交量只保留柱子，不要右轴上的最后一个值标签（会和最新价标签挤在一起）
    volume_series.last_value_visible = False
    series: list[Any] = [candle_series, volume_series]

    last_price = rows[-1]["c"]
    for level in near_levels(rows, levels):
        # 低于现价 = 买入区（绿虚线），高于现价 = 卖出区（琥珀虚线）
        color = "rgba(38,166,154,0.55)" if level <= last_price else "rgba(255,183,77,0.55)"
        candle_series.add_price_line(
            PriceLineOptions(
                price=level,
                color=color,
                line_width=1,
                line_style=LineStyle.DASHED,
                axis_label_visible=False,
            )
        )

    for line in price_lines or []:
        try:
            price = float(line.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        candle_series.add_price_line(
            PriceLineOptions(
                price=price,
                color=line.get("color", RED),
                line_width=int(line.get("width", 2)),
                line_style=LineStyle.DASHED if line.get("dashed", True) else LineStyle.SOLID,
                axis_label_visible=bool(line.get("axis_label", True)),
                title=line.get("title", ""),
                axis_label_text_color="#1b1f27",
            )
        )

    candle_series.add_price_line(
        PriceLineOptions(
            price=last_price,
            color=AMBER,
            line_width=2,
            line_style=LineStyle.SOLID,
            axis_label_visible=True,
            title="最新",
            axis_label_text_color="#1b1f27",
        )
    )

    for line in lines or []:
        points = to_line(line.get("points") or [])
        if not points:
            continue
        line_series = LineSeries(data=points)
        line_series.line_options = LineOptions(
            color=line.get("color", BLUE),
            line_width=2,
        )
        line_series.price_line_visible = False
        series.append(line_series)

    # 主图叠加指标（MA / BOLL 上下轨）：和 K 线共用右侧价格轴
    for spec in overlay_lines or []:
        points = to_line(spec.get("points") or [])
        if not points:
            continue
        overlay = LineSeries(data=points)
        overlay.line_options = LineOptions(
            color=spec.get("color", BLUE),
            line_width=int(spec.get("width", 1)),
            line_style=LineStyle.DASHED if spec.get("dashed") else LineStyle.SOLID,
        )
        overlay.price_line_visible = False
        # 指标线不显示右轴标签（数值和名字都不显示）：BOLL 三条 + MA 三条会把「最新价」淹没
        overlay.last_value_visible = False
        series.append(overlay)

    # 副图指标（MACD / RSI / KDJ）：独立 pane + 独立价格轴
    for pane in sub_panes or []:
        pane_id = int(pane.get("pane", 2))
        scale_id = f"pane{pane_id}"
        first_line: Any = None
        hist = pane.get("hist")
        if hist:
            colors = list(hist.get("colors") or [])
            bars: list[HistogramData] = []
            for index, point in enumerate(hist.get("points") or []):
                ts = _seconds(point[0]) if len(point) > 1 else None
                value = _finite(point[1]) if len(point) > 1 else None
                if ts is None or value is None:
                    continue
                color = colors[index] if index < len(colors) else "rgba(239,83,80,0.45)"
                bars.append(HistogramData(time=ts, value=value, color=color))
            if bars:
                hist_series = HistogramSeries(
                    data=bars, price_scale_id=scale_id, pane_id=pane_id
                )
                hist_series.last_value_visible = False
                series.append(hist_series)
        for line in pane.get("lines") or []:
            points = to_line(line.get("points") or [])
            if not points:
                continue
            line_series = LineSeries(data=points, price_scale_id=scale_id, pane_id=pane_id)
            line_series.line_options = LineOptions(
                color=line.get("color", AMBER),
                line_width=int(line.get("width", 1)),
            )
            line_series.price_line_visible = False
            line_series.last_value_visible = False
            series.append(line_series)
            if first_line is None:
                first_line = line_series
        # 副图标注「这是哪个指标」：轴标签只保留名字、不显示数值。
        # 必须挂在**折线** series 上（柱状图挂 title 会让整块组件渲染失败），
        # 而且 legend 字段这版 JS 不渲染，只有 series.title 会出现在所属 pane 的价格轴上。
        label = pane.get("label")
        if label and first_line is not None:
            first_line.title = label

    # chartId 里带上根数：根数一变就重建图表（LWC 才会重新 fitContent 显示新窗口）；
    # 根数不变（2 秒刷新、出现新 K 线）时 id 稳定，只更新数据、不重置缩放。
    chart = StableChart(
        series=series,
        options=build_options(interval, height),
        chart_id=f"{chart_id}-{len(rows)}",
    )
    chart.force_reinit = bool(force_reinit)
    marker_list = to_markers(markers)
    if marker_list:
        candle_series.markers = marker_list
    return chart


def render_candles(
    candles: Iterable[dict] | None,
    levels: Iterable[float] | None = None,
    lines: Iterable[dict] | None = None,
    markers: Iterable[dict] | None = None,
    interval: str = "1m",
    height: int = 520,
    key: str = "live_candles",
    price_lines: Iterable[dict] | None = None,
    overlay_lines: Iterable[dict] | None = None,
    sub_panes: Iterable[dict] | None = None,
    force_reinit: bool = False,
) -> bool:
    """渲染 K 线；key 必须稳定，否则每次 rerun 都会重建组件（缩放/平移丢失）。

    key 里带上根数：根数一变，Streamlit 会销毁旧组件、建一个新组件并重新 fitContent，
    否则同一个组件内部换数据时，LWC 会保留原来的可见时间窗 —— 表现就是"改了根数没反应"。
    根数不变时（2 秒刷新、来一根新 K 线）key 稳定，只更新数据、不重置缩放。

    `force_reinit`：这版组件**没有增量更新路径**（JS 只在首次挂载或 forceReinit 时重建），
    行情要动起来只能重建。调用方负责节流，避免每 2 秒重建一次。
    """
    rows = clean_rows(candles)
    if not rows:
        return False
    render_key = f"{key}:{len(rows)}"
    chart = build_chart(
        rows,
        levels,
        lines,
        markers,
        interval,
        height,
        price_lines,
        chart_id=render_key,
        overlay_lines=overlay_lines,
        sub_panes=sub_panes,
        force_reinit=force_reinit,
    )
    if chart is None:
        return False
    chart.render(key=render_key)
    return True
