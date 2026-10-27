"""Streamlit 前端：实时 K 线 + 网格线 + 多策略回测（网格 / 马丁格尔 / 趋势跟踪）。

交互设计要点：
1. 两级信息架构：标签页拆「实时行情 / 回测分析」，侧边栏只放参数，主区只放结论；
2. 策略可切换，参数表单按策略动态生成（注册表驱动，前端不写死字段）；
3. 参数健康度：网格间距 vs 双边手续费实时对比，间距不够直接红牌；
4. 暂停刷新、一键重置区间、回测结果指纹校验（参数变了就提示"旧结果"）；
5. 成交点回填 K 线，趋势策略叠加均线，马丁格尔标注加仓点；
6. 「三策略同台对比」用同一段行情横评，直接回答"哪个策略更好"。

数据获取采用三级降级，保证教室演示时永远有图可看：
    后端 REST 实时 → 本地 CSV 缓存（后端预热时落盘） → 本地合成行情（Mock）
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.direct_feed import fetch_klines as fetch_direct_klines_raw  # noqa: E402
from core.news import merge_news, parse_rss  # noqa: E402
from core.sentiment import composite_sentiment  # noqa: E402
from core.strategy_registry import STRATEGIES, STRATEGY_KEYS  # noqa: E402
from core.strategy_registry import run_backtest as run_strategy_backtest  # noqa: E402
from core.synthetic import default_base_price, synthesize_candles  # noqa: E402
from core.timeframe import interval_label, interval_ms  # noqa: E402

DATA_DIR = ROOT / "data"
DEFAULT_BACKEND = "http://127.0.0.1:8000"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
DEFAULT_INTERVALS = ["1m", "5m"]

PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": True}

SOURCE_COLORS = {"🟢 后端实时": "#26a69a", "🟡 本地缓存": "#ffb74d", "🔴 离线模拟": "#ef5350"}
STRATEGY_COLORS = {"grid": "#4fc3f7", "martingale": "#ffb74d", "trend": "#26a69a"}
GREEN, RED, AMBER, BLUE, GREY = "#26a69a", "#ef5350", "#ffb74d", "#4fc3f7", "#9e9e9e"

st.set_page_config(page_title="网格套利模拟 · Grid Quant", page_icon="📈", layout="wide")

st.markdown(
    """
<style>
  .block-container {padding-top: 1.2rem; padding-bottom: 2.2rem; max-width: 1560px;}
  [data-testid="stAppDeployButton"], #MainMenu, footer {visibility: hidden;}
  [data-testid="stMetric"] {
      background: rgba(255,255,255,0.035);
      border: 1px solid rgba(255,255,255,0.075);
      border-radius: 14px;
      padding: 12px 16px;
      min-height: 92px;
  }
  [data-testid="stMetricValue"] {font-size: 1.5rem; font-variant-numeric: tabular-nums;}
  [data-testid="stMetricLabel"] p {font-size: 0.78rem; opacity: 0.72; letter-spacing: 0.02em;}
  [data-testid="stSidebar"] {border-right: 1px solid rgba(255,255,255,0.06);}
  [data-testid="stSidebar"] .stButton button {font-size: 0.84rem;}
  .stTabs [data-baseweb="tab-list"] {gap: 6px;}
  .stTabs [data-baseweb="tab"] {height: 42px; padding: 0 18px; border-radius: 10px 10px 0 0;}
  div[data-testid="stHorizontalBlock"] {gap: 0.65rem;}
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- 基础工具
def get_setting(name: str, default: str = "") -> str:
    """优先读 Streamlit secrets（云部署），其次环境变量，最后默认值。"""
    try:
        if name in st.secrets:  # type: ignore[operator]
            return str(st.secrets[name])
    except Exception:  # noqa: BLE001 - 本地没有 secrets.toml 时 st.secrets 可能抛错
        pass
    return os.getenv(name, default)


@st.cache_data(ttl=30, show_spinner=False)
def fetch_config(base_url: str) -> dict:
    try:
        response = requests.get(f"{base_url}/api/config", timeout=2.5)
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001
        return {}


def fetch_latest(base_url: str, symbol: str, interval: str, limit: int, timeout: float = 2.5) -> dict:
    response = requests.get(
        f"{base_url}/api/latest_data",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_sentiment(base_url: str, symbol: str) -> dict | None:
    try:
        response = requests.get(
            f"{base_url}/api/sentiment", params={"symbol": symbol}, timeout=12.0
        )
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(base_url: str, limit: int = 8) -> dict | None:
    try:
        response = requests.get(f"{base_url}/api/news", params={"limit": limit}, timeout=12.0)
        response.raise_for_status()
        return response.json()
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_direct_sentiment(symbol: str) -> dict | None:
    """后端不可用时，前端进程直接取恐惧贪婪指数（资金费/多空比仍需后端）。"""
    try:
        response = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": 30, "format": "json"},
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json().get("data") or []
        history = [
            {"t": int(item["timestamp"]) * 1000, "value": float(item["value"])}
            for item in reversed(payload)
        ]
        if not history:
            return None
        result = composite_sentiment(fear_greed=history[-1]["value"])
        result.update(
            {
                "symbol": symbol,
                "history": history,
                "offline": False,
                "partial": True,
                "source": "alternative.me（前端直连；资金费/多空比需后端）",
                "updated_at": _iso_now(),
            }
        )
        return result
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_direct_news(limit: int = 8) -> dict | None:
    """后端不可用时，前端进程直接聚合 CoinDesk / Cointelegraph RSS。"""
    feeds = (
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
    )
    items: list[dict] = []
    for source, url in feeds:
        try:
            response = requests.get(url, timeout=8.0)
            response.raise_for_status()
            items.extend(parse_rss(response.text, source))
        except Exception:  # noqa: BLE001
            continue
    if not items:
        return None
    return {
        "items": merge_news(items, limit),
        "offline": False,
        "source": "CoinDesk / Cointelegraph RSS（前端直连）",
        "updated_at": _iso_now(),
    }


def load_local_candles(symbol: str, interval: str, limit: int) -> list[dict]:
    """读取后端预热时落盘的 CSV（断网兜底）。"""
    path = DATA_DIR / f"cache_{symbol}_{interval}.csv"
    if not path.exists():
        return []
    try:
        frame = pd.read_csv(path)
        frame = frame.dropna().sort_values("t").tail(limit)
        if frame.empty:
            return []
        return [
            {
                "t": int(row.t),
                "o": float(row.o),
                "h": float(row.h),
                "l": float(row.l),
                "c": float(row.c),
                "v": float(getattr(row, "v", 0.0) or 0.0),
                "closed": True,
            }
            for row in frame.itertuples(index=False)
        ]
    except Exception:  # noqa: BLE001
        return []


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


@st.cache_data(ttl=5, show_spinner=False)
def fetch_direct_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """前端进程直连 Binance 公共 REST（给"没有后端"的云端部署兜底）。"""
    return fetch_direct_klines_raw(symbol, interval, limit)


def resolve_data(base_url: str, symbol: str, interval: str, limit: int, offline: bool) -> dict:
    """降级链：后端实时 → Binance 直连 → 本地缓存 → 合成行情。永远返回可用数据。"""
    note: str | None = None
    now = time.time()
    backend_down = st.session_state.get("_backend_down_until", 0.0) > now

    if offline:
        note = "已手动开启离线模式（Mock），不访问后端"
    elif backend_down:
        note = "后端暂不可达，使用直连行情（60 秒后重试后端）"
    else:
        try:
            payload = fetch_latest(base_url, symbol, interval, limit)
            st.session_state["_backend_down_until"] = 0.0
            payload["ui_source"] = "🟢 后端实时"
            payload["ui_note"] = None
            return payload
        except Exception as exc:  # noqa: BLE001
            st.session_state["_backend_down_until"] = now + 60.0
            note = f"后端不可达（{type(exc).__name__}），已切换到 Binance 直连"

    if not offline:
        direct = fetch_direct_klines(symbol, interval, limit)
        if direct:
            return {
                "symbol": symbol,
                "interval": interval,
                "price": float(direct[-1]["c"]),
                "candles": direct,
                "count": len(direct),
                "stale": False,
                "age_s": 0.0,
                "source": "🟢 Binance 直连",
                "ui_source": "🟢 Binance 直连",
                "ui_note": f"{note}；直连模式只提供行情（情绪/新闻需后端）",
            }

    candles = load_local_candles(symbol, interval, limit)
    if candles:
        ui_source = "🟡 本地缓存"
        updated = datetime.fromtimestamp(candles[-1]["t"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        note = f"{note}；缓存数据截止 {updated}" if note else f"缓存数据截止 {updated}"
    else:
        candles = synthesize_candles(symbol, interval, limit)
        ui_source = "🔴 离线模拟"
        note = f"{note}；本地无缓存，使用合成行情（非真实价格）" if note else "本地无缓存，使用合成行情（非真实价格）"

    return {
        "symbol": symbol,
        "interval": interval,
        "price": float(candles[-1]["c"]),
        "candles": candles,
        "count": len(candles),
        "stale": ui_source != "🟢 后端实时",
        "age_s": None,
        "source": ui_source,
        "ui_source": ui_source,
        "ui_note": note,
    }


@st.cache_data(ttl=4, show_spinner=False)
def probe_price(base_url: str, symbol: str, interval: str, offline: bool) -> float:
    """侧边栏默认区间用的参考价；缓存几秒，避免每次拖动控件都打后端。"""
    try:
        return float(resolve_data(base_url, symbol, interval, 2, offline)["price"])
    except Exception:  # noqa: BLE001
        return default_base_price(symbol)


def plotly_chart(fig: go.Figure) -> None:
    """兼容新旧 Streamlit：1.49+ 用 width='stretch'，老版本回退到 use_container_width。"""
    try:
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
    except TypeError:
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)


def show_table(frame: pd.DataFrame, column_config: dict | None = None) -> None:
    kwargs = {"hide_index": True}
    if column_config:
        kwargs["column_config"] = column_config
    try:
        st.dataframe(frame, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(frame, use_container_width=True, **kwargs)


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def grid_levels(lower: float, upper: float, count: int) -> list[float]:
    step = (upper - lower) / count
    return [lower + index * step for index in range(count + 1)]


def status_pill(text: str, color: str) -> str:
    return (
        "<span style='display:inline-block;background:"
        f"{color}1f;color:{color};border:1px solid {color}59;border-radius:999px;"
        "padding:4px 11px;margin:0 6px 6px 0;font-size:0.82rem;font-weight:600;"
        f"white-space:nowrap'>{text}</span>"
    )


def params_fingerprint(strategy: str, symbol: str, interval: str, params: dict) -> str:
    """参数指纹：只对"策略 + 标的 + 参数"负责。

    行情本身会随实时推送变化，那属于正常的市场时间流逝，不该反复弹"旧结果"警告；
    真正需要提醒的是"你改了参数却没重跑"。
    """
    raw = "|".join(
        [
            strategy,
            symbol,
            interval,
            json.dumps(params, sort_keys=True, ensure_ascii=False, default=str),
        ]
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def ma_points(candles: list[dict], window: int) -> list[list[float]]:
    """前端计算均线，用于实时图叠加（与回测引擎的 SMA 口径一致）。"""
    if window < 2 or len(candles) < window:
        return []
    series = pd.Series([candle["c"] for candle in candles]).rolling(window).mean()
    return [
        [candles[index]["t"], float(value)]
        for index, value in enumerate(series)
        if pd.notna(value)
    ]


# ---------------------------------------------------------------- 图表
def build_candle_figure(
    candles: list[dict],
    levels: list[float],
    symbol: str,
    interval: str,
    height: int = 520,
    uirevision: str = "live",
    lines: list[dict] | None = None,
) -> go.Figure:
    frame = pd.DataFrame(candles)
    frame["time"] = pd.to_datetime(frame["t"], unit="ms")
    rising = frame["c"] >= frame["o"]
    last_price = float(frame["c"].iloc[-1])

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.77, 0.23],
    )
    figure.add_trace(
        go.Candlestick(
            x=frame["time"],
            open=frame["o"],
            high=frame["h"],
            low=frame["l"],
            close=frame["c"],
            name=symbol,
            increasing=dict(line=dict(color=GREEN, width=1), fillcolor=GREEN),
            decreasing=dict(line=dict(color=RED, width=1), fillcolor=RED),
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=frame["time"],
            y=frame["v"],
            name="成交量",
            marker_color=["rgba(38,166,154,0.40)" if up else "rgba(239,83,80,0.40)" for up in rising],
            hovertemplate="成交量 %{y:,.2f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    # 网格线按"在当前价之上/之下"着色：下方=买入区，上方=卖出区
    for level in levels:
        color = "rgba(38,166,154,0.34)" if level <= last_price else "rgba(255,183,77,0.30)"
        figure.add_hline(y=level, line=dict(color=color, width=1, dash="dot"), row=1, col=1)

    for line in lines or []:
        points = line.get("points") or []
        if not points:
            continue
        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime([point[0] for point in points], unit="ms"),
                y=[point[1] for point in points],
                mode="lines",
                name=line.get("name", "均线"),
                line=dict(color=line.get("color", BLUE), width=1.4),
                hovertemplate=line.get("name", "均线") + " %{y:,.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    figure.add_hline(
        y=last_price,
        line=dict(color=AMBER, width=1.4),
        annotation_text=f" 最新 {last_price:,.2f} ",
        annotation_position="top left",
        annotation_font=dict(color=AMBER, size=11),
        row=1,
        col=1,
    )

    figure.update_layout(
        template="plotly_dark",
        height=height,
        margin=dict(l=8, r=10, t=64, b=6),
        title=dict(
            text=f"<b>{symbol}</b> · {interval_label(interval)} · {len(levels)} 条参考线"
            if levels
            else f"<b>{symbol}</b> · {interval_label(interval)}",
            font=dict(size=14),
            x=0.005,
            y=0.985,
            yanchor="top",
        ),
        showlegend=bool(lines),
        legend=dict(orientation="h", y=1.04, x=0, font=dict(size=11)),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        bargap=0.1,
        uirevision=uirevision,
    )
    if interval == "1d":
        range_buttons = [
            dict(count=7, label="7d", step="day", stepmode="backward"),
            dict(count=30, label="30d", step="day", stepmode="backward"),
            dict(step="all", label="全部"),
        ]
    elif interval == "1w":
        # Plotly 的 rangeselector 不支持 step="week"，用天数换算：12 周 ≈ 84 天，52 周 ≈ 364 天
        range_buttons = [
            dict(count=84, label="12w", step="day", stepmode="backward"),
            dict(count=364, label="1y", step="day", stepmode="backward"),
            dict(step="all", label="全部"),
        ]
    elif interval == "1M":
        range_buttons = [
            dict(count=12, label="12M", step="month", stepmode="backward"),
            dict(count=36, label="36M", step="month", stepmode="backward"),
            dict(step="all", label="全部"),
        ]
    elif interval == "1y":
        range_buttons = [
            dict(count=5, label="5y", step="year", stepmode="backward"),
            dict(step="all", label="全部"),
        ]
    else:
        range_buttons = [
            dict(count=30, label="30m", step="minute", stepmode="backward"),
            dict(count=2, label="2h", step="hour", stepmode="backward"),
            dict(step="all", label="全部"),
        ]

    figure.update_xaxes(
        showgrid=False,
        rangeselector=dict(
            buttons=range_buttons,
            bgcolor="rgba(255,255,255,0.05)",
            activecolor="rgba(79,195,247,0.35)",
            font=dict(size=10, color="#c9d1d9"),
            x=1,
            xanchor="right",
            y=1.02,
            yanchor="bottom",
        ),
        row=1,
        col=1,
    )
    figure.update_yaxes(side="right", row=1, col=1)
    figure.update_yaxes(side="right", row=2, col=1, showgrid=False)

    # 纵轴以"实际价格走势"为主，只把附近的网格线纳入视野：
    # 否则当网格区间远大于当日振幅时，蜡烛会被压成一条直线，完全看不清形态。
    low_c = float(frame["l"].min())
    high_c = float(frame["h"].max())
    band = (high_c - low_c) * 0.4 or last_price * 0.002
    near_levels = [level for level in levels if low_c - band <= level <= high_c + band]
    lo = min([low_c, *near_levels]) if near_levels else low_c
    hi = max([high_c, *near_levels]) if near_levels else high_c
    pad = (hi - lo) * 0.03 or last_price * 0.002
    figure.update_yaxes(range=[lo - pad, hi + pad], row=1, col=1)

    return figure


def build_trade_figure(candles: list[dict], result: dict, symbol: str, interval: str) -> go.Figure:
    """K 线 + 参考线 + 每笔成交点：让策略行为肉眼可见。"""
    overlays = result.get("overlays") or {}
    levels = overlays.get("grid_levels") or (result.get("grid") or {}).get("levels", [])
    lines = overlays.get("lines") or []

    figure = build_candle_figure(
        candles,
        levels,
        symbol,
        interval,
        height=470,
        uirevision=f"trade-{symbol}-{interval}",
        lines=lines,
    )

    trades = result["trades"]
    for side, color, marker_symbol, label in (
        ("buy", GREEN, "triangle-up", "买入"),
        ("sell", RED, "triangle-down", "卖出"),
    ):
        rows = [trade for trade in trades if trade["side"] == side]
        if not rows:
            continue
        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime([trade["t"] for trade in rows], unit="ms"),
                y=[trade["price"] for trade in rows],
                mode="markers",
                name=f"{label} {len(rows)}",
                marker=dict(
                    symbol=marker_symbol,
                    size=9,
                    color=color,
                    line=dict(color="rgba(0,0,0,0.55)", width=0.8),
                ),
                customdata=[[trade.get("reason") or ""] for trade in rows],
                hovertemplate=f"{label} %{{y:,.2f}} · %{{customdata[0]}}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    figure.update_layout(title_text="", showlegend=True, legend=dict(orientation="h", y=1.04, x=0))
    return figure


def build_equity_figure(equity_curve: list[dict]) -> go.Figure:
    frame = pd.DataFrame(equity_curve)
    frame["time"] = pd.to_datetime(frame["t"], unit="ms")
    frame["peak"] = frame["equity"].cummax()
    frame["drawdown"] = frame["equity"] / frame["peak"] - 1.0

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.66, 0.34],
        subplot_titles=("策略净值曲线", "回撤"),
    )
    figure.add_trace(
        go.Scatter(
            x=frame["time"],
            y=frame["equity"],
            mode="lines",
            name="净值",
            line=dict(color=BLUE, width=2),
            hovertemplate="净值 %{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["time"],
            y=frame["drawdown"] * 100,
            mode="lines",
            name="回撤 %",
            line=dict(color=RED, width=1.2),
            fill="tozeroy",
            fillcolor="rgba(239,83,80,0.18)",
            hovertemplate="回撤 %{y:.2f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )
    figure.update_layout(
        template="plotly_dark",
        height=420,
        margin=dict(l=8, r=10, t=46, b=6),
        showlegend=False,
        hovermode="x unified",
        uirevision="equity",
    )
    figure.update_yaxes(side="right", row=1, col=1)
    figure.update_yaxes(side="right", row=2, col=1, ticksuffix="%")

    equity = frame["equity"]
    low, high = float(equity.min()), float(equity.max())
    span = (high - low) or max(abs(high) * 0.001, 1.0)
    figure.update_yaxes(range=[low - span * 0.15, high + span * 0.15], row=1, col=1)
    return figure


def build_compare_figure(curves: dict[str, list[dict]], initial_capital: float) -> go.Figure:
    figure = go.Figure()
    for key, curve in curves.items():
        frame = pd.DataFrame(curve)
        if frame.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime(frame["t"], unit="ms"),
                y=(frame["equity"] / initial_capital - 1.0) * 100,
                mode="lines",
                name=f"{STRATEGIES[key].icon} {STRATEGIES[key].label}",
                line=dict(color=STRATEGY_COLORS.get(key, BLUE), width=2),
                hovertemplate="%{y:.2f}%<extra></extra>",
            )
        )
    figure.update_layout(
        template="plotly_dark",
        height=430,
        margin=dict(l=8, r=10, t=40, b=6),
        yaxis_title="累计收益率 %",
        hovermode="x unified",
        legend=dict(orientation="h", y=1.02, x=0),
        uirevision="compare",
    )
    figure.update_yaxes(side="right", ticksuffix="%")
    return figure


def build_sentiment_gauge(score: float, label: str) -> go.Figure:
    color = RED if score >= 75 else AMBER if score >= 55 else GREY if score >= 45 else GREEN
    figure = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            number={"font": {"size": 40}, "suffix": ""},
            title={"text": f"FOMO 情绪 · {label}", "font": {"size": 15}},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#9e9e9e"},
                "bar": {"color": color, "thickness": 0.28},
                "bgcolor": "rgba(255,255,255,0.03)",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 25], "color": "rgba(38,166,154,0.28)"},
                    {"range": [25, 45], "color": "rgba(38,166,154,0.14)"},
                    {"range": [45, 55], "color": "rgba(158,158,158,0.14)"},
                    {"range": [55, 75], "color": "rgba(255,183,77,0.18)"},
                    {"range": [75, 100], "color": "rgba(239,83,80,0.26)"},
                ],
                "threshold": {
                    "line": {"color": "white", "width": 2},
                    "thickness": 0.8,
                    "value": score,
                },
            },
        )
    )
    figure.update_layout(
        template="plotly_dark",
        height=280,
        margin=dict(l=30, r=30, t=56, b=10),
    )
    return figure


def build_sentiment_history(history: list[dict]) -> go.Figure:
    frame = pd.DataFrame(history)
    frame["time"] = pd.to_datetime(frame["t"], unit="ms")
    figure = go.Figure(
        go.Scatter(
            x=frame["time"],
            y=frame["value"],
            mode="lines+markers",
            name="恐惧贪婪指数",
            line=dict(color=AMBER, width=2),
            marker=dict(size=4),
            hovertemplate="%{y:.0f}<extra></extra>",
        )
    )
    figure.add_hline(y=75, line=dict(color=RED, width=1, dash="dot"), annotation_text="贪婪 75")
    figure.add_hline(y=25, line=dict(color=GREEN, width=1, dash="dot"), annotation_text="恐惧 25")
    figure.update_layout(
        template="plotly_dark",
        height=260,
        margin=dict(l=8, r=10, t=20, b=6),
        yaxis=dict(range=[0, 100], title="指数"),
        showlegend=False,
        hovermode="x unified",
        uirevision="sentiment-history",
    )
    return figure


# ---------------------------------------------------------------- 回测结果
def render_backtest(result: dict, meta: dict, candles: list[dict]) -> None:
    summary = result["summary"]
    strategy_label = result.get("strategy_label") or STRATEGIES.get(result.get("strategy", ""), None)
    strategy_key = result.get("strategy", "grid")

    st.markdown(f"##### 📊 绩效指标 · {result.get('strategy_icon', '')} {strategy_label}")

    primary = st.columns(5)
    primary[0].metric(
        "总收益率",
        f"{summary['total_return'] * 100:+.2f}%",
        delta=f"{summary['net_pnl']:+,.2f} USDT",
        delta_color="normal",
        help="期末权益（含未平仓持仓市值）/ 初始资金 − 1",
    )
    primary[1].metric("最大回撤", f"{summary['max_drawdown'] * 100:.2f}%", help="净值相对历史峰值的最大跌幅")
    primary[2].metric(
        "夏普比率",
        f"{summary['sharpe']:.2f}",
        help="逐 K 线收益均值 / 标准差 × sqrt(一年 K 线数)；1m 粒度年化系数约 ×725，短样本会放大",
    )
    if summary.get("closed_trade_count", 0) > 0:
        primary[3].metric("胜率", f"{summary['win_rate'] * 100:.1f}%", help="仅统计已平仓（已卖出）交易")
    else:
        primary[3].metric("胜率", "—", help="还没有平仓交易，胜率无意义（例如趋势策略仍在持仓中）")
    primary[4].metric("交易笔数", f"{summary['trade_count']}", help="买入 + 卖出总笔数")

    profit_factor = summary["profit_factor"]
    if profit_factor is not None:
        profit_text = f"{profit_factor:.2f}"
    elif summary.get("closed_trade_count"):
        profit_text = "∞（无亏损单）"
    else:
        profit_text = "—"

    sample_days = summary["sample_bars"] * interval_ms(result["params"]["interval"]) / 86_400_000

    with st.container(border=True):
        secondary = st.columns(5)
        secondary[0].metric("期末权益", fmt_money(summary["final_equity"]))
        secondary[1].metric("手续费合计", fmt_money(summary["total_fees"]), help="双边手续费累计，直接侵蚀策略利润")
        secondary[2].metric("盈亏比", profit_text, help="总盈利 / 总亏损")
        if sample_days >= 7:
            secondary[3].metric(
                "年化收益*",
                f"{summary['annualized_return'] * 100:+.1f}%",
                help="按复利把回测区间收益折算成年化；样本越长越可信",
            )
        else:
            secondary[3].metric(
                "年化收益*",
                "—",
                help=f"样本仅 {sample_days:.2f} 天（< 7 天），年化折算没有统计意义，故不展示",
            )
        highlight = result.get("highlight") or {}
        secondary[4].metric(
            highlight.get("label", "策略亮点"),
            str(highlight.get("value", "—")),
            help=highlight.get("help") or "策略专属指标",
        )

    st.caption(
        f"样本 {summary['sample_bars']} 根 K 线（{summary['start_time']} → {summary['end_time']}）"
        f" · 数据来源：{meta.get('source', '未知')} · 周期：{interval_label(result['params']['interval'])}"
        f" · 执行：{'合约 ' + str(int(summary.get('leverage', 1))) + 'x' if summary.get('mode') == 'futures' else '现货'}"
        f" · {'多空双向' if (result.get('meta') or {}).get('direction') == 'long_short' else '仅做多'}"
        f" · {result.get('tagline', '')}"
        " · *样本短于 7 天时年化收益不展示；夏普已被年化系数放大，仅供横向比较。"
    )
    extra_bits: list[str] = []
    if summary.get("liquidation_count"):
        extra_bits.append(f"强平 {summary['liquidation_count']} 笔")
    if (summary.get("funding_paid") or 0.0) > 1e-6:
        extra_bits.append(f"资金费支出 {summary['funding_paid']:,.2f} USDT")
    if extra_bits:
        st.caption(" · ".join(extra_bits))
    if summary.get("volatility_drag"):
        st.info(
            "夏普为正但净值大幅回撤：这是波动率拖累（volatility drag）——"
            "算术平均收益掩盖了几何复利的损失。高杠杆下这一点尤其致命，请以净值曲线为准。",
            icon="📉",
        )

    for warning in result["warnings"]:
        st.warning(warning, icon="⚠️")

    st.markdown("##### 🎯 成交分布（买卖点回填到 K 线）")
    symbol = meta.get("symbol") or result.get("symbol") or ""
    plotly_chart(build_trade_figure(candles, result, symbol, result["params"]["interval"]))

    st.markdown("##### 📈 净值与回撤")
    plotly_chart(build_equity_figure(result["equity_curve"]))

    trades = result["trades"]
    with st.expander(f"📋 交易明细（{len(trades)} 笔）", expanded=False):
        if not trades:
            st.info("本次回测没有产生任何成交。", icon="💤")
        else:
            frame = pd.DataFrame(trades)
            frame["side_label"] = frame["side"].map({"buy": "买入", "sell": "卖出"})
            has_reason = "reason" in frame.columns and frame["reason"].notna().any()
            has_position = "position" in frame.columns and frame["position"].notna().any()
            columns = ["time", "side_label", "price", "qty", "notional", "fee", "pnl", "entry_price", "entry_time"]
            rename = {
                "time": "时间",
                "side_label": "方向",
                "price": "成交价",
                "qty": "数量",
                "notional": "成交额",
                "fee": "手续费",
                "pnl": "平仓盈亏",
                "entry_price": "开仓价",
                "entry_time": "开仓时间",
            }
            if has_position:
                frame["position_label"] = frame["position"].map({"long": "多", "short": "空"})
                columns.insert(2, "position_label")
                rename["position_label"] = "持仓"
            if has_reason:
                columns.append("reason")
                rename["reason"] = "备注"
            frame = frame[columns].rename(columns=rename)

            choice = st.radio(
                "筛选",
                ["全部", "只看买入", "只看卖出"],
                horizontal=True,
                label_visibility="collapsed",
                key=f"filter_{strategy_key}",
            )
            view = frame
            if choice == "只看买入":
                view = frame[frame["方向"] == "买入"]
            elif choice == "只看卖出":
                view = frame[frame["方向"] == "卖出"]
            view = view.copy()
            # 买入单没有平仓盈亏/开仓价：格式化成空串，避免表格里出现 None
            view["平仓盈亏"] = view["平仓盈亏"].map(lambda v: f"{v:,.2f}" if pd.notna(v) else "")
            view["开仓价"] = view["开仓价"].map(lambda v: f"{v:,.2f}" if pd.notna(v) else "")
            view["开仓时间"] = view["开仓时间"].fillna("—")
            show_table(
                view,
                {
                    "成交价": st.column_config.NumberColumn(format="%.2f"),
                    "数量": st.column_config.NumberColumn(format="%.6f"),
                    "成交额": st.column_config.NumberColumn(format="%.2f"),
                    "手续费": st.column_config.NumberColumn(format="%.4f"),
                },
            )
            st.download_button(
                "⬇️ 下载全部交易明细（CSV）",
                frame.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{strategy_key}_trades_{symbol}_{result['params']['interval']}.csv",
                mime="text/csv",
            )

    with st.expander("🧾 回测假设与口径（答辩前请读一遍）", expanded=False):
        for assumption in (result.get("meta") or {}).get("assumptions", []):
            st.markdown(f"- {assumption}")
        st.json(result.get("meta", {}), expanded=False)


def render_empty_state() -> None:
    st.markdown("##### 还没有回测结果")
    columns = st.columns(3)
    steps = [
        ("① 选策略", "左侧切换网格 / 马丁格尔 / 趋势跟踪，参数表单会跟着变。"),
        ("② 调参数", "网格看间距与手续费；马丁格尔看层数与倍数；趋势看快慢线与止损。"),
        ("③ 跑回测", "点「运行回测」看收益率、回撤、夏普、胜率，以及买卖点回填到 K 线。"),
    ]
    for column, (title, text) in zip(columns, steps):
        with column:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(text)


# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.markdown("### ⚙️ 参数配置")
    st.caption("所有改动即时生效；改动参数后请重新运行回测。")

    backend_default = st.session_state.get("backend_url", get_setting("BACKEND_BASE_URL", DEFAULT_BACKEND))
    backend_url = (
        st.text_input(
            "后端地址",
            value=backend_default,
            key="backend_input",
            help="云端部署时填 https:// 公网地址；本地默认 127.0.0.1:8000",
        )
        .strip()
        .rstrip("/")
    )
    st.session_state["backend_url"] = backend_url

    config = fetch_config(backend_url)
    symbols = config.get("symbols") or DEFAULT_SYMBOLS
    intervals = config.get("intervals") or DEFAULT_INTERVALS
    default_symbol = config.get("default_symbol") or symbols[0]
    default_interval = get_setting("DEFAULT_INTERVAL", config.get("default_interval") or intervals[0])
    if default_interval not in intervals:
        default_interval = intervals[0]

    pick_left, pick_right = st.columns(2)
    with pick_left:
        symbol = st.selectbox(
            "交易对",
            symbols,
            index=symbols.index(default_symbol) if default_symbol in symbols else 0,
        )
    with pick_right:
        interval = st.selectbox(
            "K 线周期",
            intervals,
            index=intervals.index(default_interval) if default_interval in intervals else 0,
        )

    offline = st.toggle("离线模式（Mock）", value=False, help="不访问后端，直接用缓存 / 合成行情演示")
    limit = st.slider("显示 K 线根数", min_value=100, max_value=1000, value=300, step=50)
    paused = st.toggle(
        "暂停实时刷新",
        value=False,
        help="讲解时冻结画面：图表不再每 2 秒自动刷新，参数调整仍立即生效",
    )

    price = probe_price(backend_url, symbol, interval, offline)

    st.divider()
    st.markdown("#### 🧠 策略选择")
    default_strategy = get_setting("DEFAULT_STRATEGY", "grid").lower()
    if default_strategy not in STRATEGIES:
        default_strategy = "grid"
    strategy_key = st.selectbox(
        "策略",
        STRATEGY_KEYS,
        index=STRATEGY_KEYS.index(default_strategy),
        format_func=lambda key: f"{STRATEGIES[key].icon} {STRATEGIES[key].label}",
        help="不同策略有各自的参数与风险特征；切换后下方参数表单会同步变化",
    )
    strategy_spec = STRATEGIES[strategy_key]
    st.caption(strategy_spec.tagline)

    st.markdown("##### 🌐 市场模式")
    mode_default = get_setting("DEFAULT_MODE", "spot").lower()
    trading_mode = st.radio(
        "市场模式",
        ["spot", "futures"],
        index=1 if mode_default == "futures" else 0,
        format_func=lambda value: "现货（无杠杆）" if value == "spot" else "合约（可加杠杆）",
        horizontal=True,
        help="现货：无杠杆、无强平；合约：保证金交易，按杠杆放大名义价值，价格反向波动会触发强平",
    )
    leverage = 1.0
    funding_rate = 0.0
    allow_short = False
    if trading_mode == "futures":
        try:
            default_leverage = int(get_setting("DEFAULT_LEVERAGE", "5") or 5)
        except ValueError:
            default_leverage = 5
        default_leverage = max(1, min(default_leverage, 20))
        leverage = float(
            st.slider(
                "杠杆倍数",
                1,
                20,
                default_leverage,
                1,
                help="杠杆越高，强平线越近；20x 时价格反向约 4.5% 即爆仓",
            )
        )
        funding_rate = float(
            st.number_input(
                "资金费率 / 8h",
                min_value=0.0,
                max_value=0.005,
                value=0.0001,
                step=0.0001,
                format="%.4f",
                help="多头支付、空头收取；按净头寸名义价值每 8 小时结算",
            )
        )
        allow_short = st.toggle(
            "允许做空（趋势策略）",
            value=True,
            help="开启后：均线空头排列时开空、涨回上方平空；关闭则只做多。网格/马丁格尔始终只做多。",
        )
        st.caption(
            f"⚠️ 多头强平线约在开仓价 −{(1.0 / leverage - 0.005) * 100:.2f}% · "
            f"空头强平线约在 +{(1.0 / leverage - 0.005) * 100:.2f}%"
        )
    else:
        st.caption("现货模式：1:1 保证金，无强平、无资金费率、不可做空。")

    direction = "long_short" if (trading_mode == "futures" and allow_short) else "long_only"

    if interval in ("1w", "1M", "1y"):
        st.caption(
            "长周期样本有限：1y 由 1M 聚合（BTC 自 2017 年至今约 9 根），"
            "趋势策略请把均线周期调小（例如 3 / 8）。"
        )

    # 重置/切换标的的默认区间必须在控件创建之前写入 session_state
    if st.session_state.pop("_reset_bounds", False):
        st.session_state["grid_lower"] = round(price * 0.97, 2)
        st.session_state["grid_upper"] = round(price * 1.03, 2)

    bounds_key = f"{symbol}:{interval}"
    if st.session_state.get("_bounds_for") != bounds_key:
        st.session_state["_bounds_for"] = bounds_key
        st.session_state["grid_lower"] = round(price * 0.97, 2)
        st.session_state["grid_upper"] = round(price * 1.03, 2)

    strategy_params: dict[str, float] = {}

    if strategy_key == "grid":
        st.markdown("##### 🎯 网格区间")
        step_usdt = max(price * 0.001, 0.01)
        bound_left, bound_right = st.columns(2)
        lower = bound_left.number_input("下界 (USDT)", min_value=0.0, step=step_usdt, key="grid_lower")
        upper = bound_right.number_input("上界 (USDT)", min_value=0.0, step=step_usdt, key="grid_upper")
        grid_count = st.slider(
            "网格数量",
            min_value=2,
            max_value=100,
            value=20,
            step=1,
            help="格数越多，单格利润越薄；间距必须大于双边手续费",
        )
        strategy_params = {"lower": float(lower), "upper": float(upper), "grid_count": int(grid_count)}

        st.button(
            "↺ 以最新价重置 ±3%",
            width="stretch",
            on_click=lambda: st.session_state.update(_reset_bounds=True),
        )
    else:
        st.markdown("##### 🎛️ 策略参数")
        for param in strategy_spec.params:
            widget_key = f"sp_{strategy_key}_{param.key}"
            if param.kind == "int":
                strategy_params[param.key] = st.number_input(
                    param.label,
                    min_value=int(param.min),
                    max_value=int(param.max),
                    value=int(param.default),
                    step=max(int(param.step), 1),
                    key=widget_key,
                    help=param.help,
                )
            else:
                strategy_params[param.key] = st.number_input(
                    param.label,
                    min_value=float(param.min),
                    max_value=float(param.max),
                    value=float(param.default),
                    step=float(param.step),
                    format=param.format,
                    key=widget_key,
                    help=param.help,
                )
        st.caption(strategy_spec.description)

    with st.expander("💰 资金与费率", expanded=True):
        capital = st.number_input("初始资金 (USDT)", min_value=100.0, value=10_000.0, step=500.0)
        fee_default = 0.0005 if trading_mode == "futures" else 0.001
        fee_rate = st.number_input(
            "单边手续费率",
            min_value=0.0,
            max_value=0.005,
            value=fee_default,
            step=0.0001,
            format="%.4f",
            key=f"fee_{trading_mode}",
            help="现货 taker 约 0.1%；U 本位合约 taker 约 0.05%；双边累计",
        )

    if strategy_key == "grid":
        if upper > lower > 0:
            mid_price = (lower + upper) / 2
            step_value = (upper - lower) / grid_count
            step_pct = step_value / mid_price
            if step_pct <= fee_rate * 2:
                st.error(
                    f"间距 {step_pct * 100:.3f}% ≤ 双边手续费 {fee_rate * 200:.3f}%："
                    "每完成一个买入→卖出循环都是净亏损。请减少网格数或放宽区间。",
                    icon="🚨",
                )
            else:
                st.success(
                    f"间距 {step_pct * 100:.2f}%（{step_value:,.2f} USDT）· "
                    f"扣掉双边手续费后单格净空间 {(step_pct - fee_rate * 2) * 100:.2f}%",
                    icon="✅",
                )
        else:
            st.error("上界必须大于下界，否则无法生成网格。", icon="🚨")
    elif strategy_key == "martingale":
        total_required = 0.0
        size = float(strategy_params.get("initial_order_usdt", 0.0))
        multiplier = float(strategy_params.get("multiplier", 1.0))
        for _ in range(int(strategy_params.get("max_entries", 1))):
            total_required += size
            size *= multiplier
        if total_required > capital:
            st.error(
                f"铺满 {int(strategy_params.get('max_entries', 0))} 层需要 {total_required:,.0f} USDT > 可用资金 "
                f"{capital:,.0f}：加仓链会在中途断掉，回测会如实告警。",
                icon="🚨",
            )
        else:
            st.info(
                f"铺满全部层数需要 {total_required:,.0f} USDT（占资金 "
                f"{total_required / capital * 100:.0f}%），留有余量。",
                icon="💰",
            )
    else:
        fast_ma = int(strategy_params.get("fast_ma", 10))
        slow_ma = int(strategy_params.get("slow_ma", 30))
        if fast_ma >= slow_ma:
            st.error("慢线周期必须大于快线周期。", icon="🚨")
        else:
            st.info(
                f"{fast_ma}/{slow_ma} 均线：至少需要 {slow_ma + 2} 根 K 线才会出信号，"
                f"当前加载 {limit} 根。",
                icon="📐",
            )

    action_left, action_right = st.columns(2)
    if action_left.button("🔌 测试连接", width="stretch"):
        try:
            health = requests.get(f"{backend_url}/api/health", timeout=3).json()
            st.toast(f"连接正常 · {health.get('mode')} / {health.get('status')}", icon="✅")
        except Exception as exc:  # noqa: BLE001
            st.toast(f"连接失败：{type(exc).__name__}", icon="🚨")

    if action_right.button("♻️ 刷新缓存", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    st.caption("纯模拟盘 · 无 API Key · 无真实下单接口")


# ---------------------------------------------------------------- 主区
header_left, header_right = st.columns([3, 1])
with header_left:
    st.title("📈 加密货币量化策略回测 · 模拟交易")
    st.caption(
        "Binance 公共行情 · 网格 / 马丁格尔 / 趋势跟踪 · 纯模拟（Paper Trading），不接真实资金"
    )
with header_right:
    st.caption(f"**{symbol}** · {interval_label(interval)}")
    st.caption(f"{strategy_spec.icon} {strategy_spec.label}")
    mode_text = f"合约 {leverage:.0f}x" if trading_mode == "futures" else "现货"
    if direction == "long_short":
        mode_text += " · 双向"
    st.caption(mode_text)


def render_live_body(ctx: dict) -> None:
    payload = resolve_data(
        ctx["base_url"], ctx["symbol"], ctx["interval"], ctx["limit"], ctx["offline"]
    )
    st.session_state["_last_payload"] = payload

    candles = payload["candles"]
    strategy = ctx["strategy"]
    params = ctx["params"]

    levels: list[float] = []
    lines: list[dict] = []
    if strategy == "grid" and params.get("upper", 0) > params.get("lower", 0) > 0:
        levels = grid_levels(params["lower"], params["upper"], int(params["grid_count"]))
    elif strategy == "trend":
        fast_ma = int(params.get("fast_ma", 10))
        slow_ma = int(params.get("slow_ma", 30))
        candidate_lines = [
            {"name": f"SMA{fast_ma}", "color": BLUE, "points": ma_points(candles, fast_ma)},
            {"name": f"SMA{slow_ma}", "color": AMBER, "points": ma_points(candles, slow_ma)},
        ]
        lines = [line for line in candidate_lines if line["points"]]

    source_color = SOURCE_COLORS.get(payload["ui_source"], BLUE)
    change = (payload["price"] / candles[0]["o"] - 1) * 100 if candles else 0.0
    age = f"{payload['age_s']:.1f}s" if payload.get("age_s") is not None else "—"

    chips = [
        status_pill(payload["ui_source"].split(" ", 1)[-1], source_color),
        status_pill(f"最新价 {payload['price']:,.2f}", BLUE),
        status_pill(f"区间涨跌 {change:+.2f}%", GREEN if change >= 0 else RED),
        status_pill(f"策略 {STRATEGIES[strategy].icon} {STRATEGIES[strategy].label}", AMBER),
        status_pill(
            f"合约 {ctx['leverage']:.0f}x" if ctx["mode"] == "futures" else "现货",
            RED if ctx["mode"] == "futures" else GREY,
        ),
        status_pill(
            "双向" if ctx.get("direction") == "long_short" else "仅多",
            GREEN if ctx.get("direction") == "long_short" else GREY,
        ),
        status_pill(f"K 线 {payload['count']} 根", GREY),
        status_pill(f"延迟 {age}", GREY),
    ]
    if ctx["paused"]:
        chips.append(status_pill("⏸ 已暂停刷新", AMBER))
    st.markdown("<div style='margin:2px 0 8px 0'>" + "".join(chips) + "</div>", unsafe_allow_html=True)

    if payload.get("ui_note"):
        st.info(payload["ui_note"], icon="🛰️")

    plotly_chart(
        build_candle_figure(
            candles,
            levels,
            ctx["symbol"],
            ctx["interval"],
            uirevision=f"{ctx['symbol']}-{ctx['interval']}-{strategy}",
            lines=lines,
        )
    )

    with st.expander("🩺 后端连接详情（/api/health）", expanded=False):
        if ctx["offline"]:
            st.caption("离线模式，未请求后端。")
        elif st.session_state.get("_backend_down_until", 0.0) > time.time():
            st.caption("后端暂不可达，已切换 Binance 直连行情（60 秒后重试后端）。")
        else:
            try:
                st.json(requests.get(f"{ctx['base_url']}/api/health", timeout=2.0).json())
            except Exception as exc:  # noqa: BLE001
                st.caption(f"后端不可达：{type(exc).__name__}")


@st.fragment(run_every="2s")
def live_panel_auto(ctx: dict) -> None:
    render_live_body(ctx)


def live_panel_manual(ctx: dict) -> None:
    render_live_body(ctx)


live_ctx = {
    "base_url": backend_url,
    "symbol": symbol,
    "interval": interval,
    "limit": limit,
    "offline": offline,
    "paused": paused,
    "strategy": strategy_key,
    "params": strategy_params,
    "mode": trading_mode,
    "leverage": leverage,
    "direction": direction,
}

tab_live, tab_backtest, tab_mood = st.tabs(["📊 实时行情", "🧮 回测分析", "🧠 情绪 · 新闻"])

with tab_live:
    if paused:
        live_panel_manual(live_ctx)
    else:
        live_panel_auto(live_ctx)

with tab_backtest:
    payload = st.session_state.get("_last_payload")
    if not payload or not payload.get("candles"):
        payload = resolve_data(backend_url, symbol, interval, limit, offline)
        st.session_state["_last_payload"] = payload

    candles = payload["candles"]
    run_params = {
        **strategy_params,
        "initial_capital": float(capital),
        "fee_rate": float(fee_rate),
        "mode": trading_mode,
        "leverage": leverage,
        "funding_rate_8h": funding_rate,
        "direction": direction,
        "interval": interval,
    }
    fingerprint = params_fingerprint(strategy_key, symbol, interval, run_params)

    action_col, info_col = st.columns([1, 2.6])
    with action_col:
        run_clicked = st.button(
            "🚀 运行回测",
            type="primary",
            width="stretch",
            disabled=not candles,
        )
    with info_col:
        chip_html = (
            status_pill(f"{strategy_spec.icon} {strategy_spec.label}", STRATEGY_COLORS.get(strategy_key, BLUE))
            + status_pill(
                f"合约 {leverage:.0f}x" if trading_mode == "futures" else "现货",
                RED if trading_mode == "futures" else GREY,
            )
            + status_pill("双向" if direction == "long_short" else "仅多", GREEN if direction == "long_short" else GREY)
            + status_pill(f"资金 {capital:,.0f} USDT", GREY)
            + status_pill(f"费率 {fee_rate * 100:.2f}%", GREY)
            + status_pill(f"数据 {payload['ui_source'].split(' ', 1)[-1]}", SOURCE_COLORS.get(payload["ui_source"], GREY))
        )
        st.markdown(chip_html, unsafe_allow_html=True)
        st.caption("回测在本地运行 `core/` 引擎，不依赖后端在线；参数变化后结果会提示重新运行。")

    if run_clicked:
        try:
            with st.spinner(f"{strategy_spec.label} 回测中…"):
                result = run_strategy_backtest(strategy_key, candles, run_params)
        except (ValueError, TypeError) as exc:
            st.error(f"参数不合法：{exc}", icon="🚨")
        else:
            st.session_state["backtest_result"] = result
            st.session_state["backtest_candles"] = candles
            st.session_state["backtest_fp"] = fingerprint
            st.session_state["backtest_meta"] = {
                "symbol": symbol,
                "interval": interval,
                "source": payload["ui_source"],
            }
            st.toast(f"回测完成：{result['summary']['trade_count']} 笔成交", icon="✅")

    result = st.session_state.get("backtest_result")
    if result:
        if st.session_state.get("backtest_fp") != fingerprint:
            st.warning(
                "策略、参数或行情自上次回测后已变化，下面的结果对应**旧参数**。请重新点击「运行回测」。",
                icon="⚠️",
            )
        render_backtest(
            result,
            st.session_state.get("backtest_meta", {}),
            st.session_state.get("backtest_candles") or candles,
        )
    else:
        render_empty_state()

    # ---------------- 三策略横评 ----------------
    st.divider()
    st.markdown("##### 🏁 三策略同台对比")
    st.caption(
        "同一段行情、同一笔资金，三种策略各按自己的参数跑一遍。"
        "网格吃震荡、马丁格尔赌回归、趋势跟方向 —— 谁在这段行情里赢了，一眼可见。"
    )

    compare_fp = params_fingerprint(
        "compare", symbol, interval, {"capital": capital, "fee": fee_rate}
    )
    if st.button("▶️ 运行对比（约 1 秒）", width="stretch"):
        compare_rows: list[dict] = []
        compare_curves: dict[str, list[dict]] = {}
        errors: list[str] = []
        for key in STRATEGY_KEYS:
            params = {
                "initial_capital": float(capital),
                "fee_rate": float(fee_rate),
                "mode": trading_mode,
                "leverage": leverage,
                "funding_rate_8h": funding_rate,
                "direction": direction,
                "interval": interval,
            }
            if key == "grid" and strategy_key == "grid":
                params.update(strategy_params)
            elif key == "grid":
                params.update({"lower": price * 0.97, "upper": price * 1.03, "grid_count": 20})
            elif key == strategy_key:
                params.update(strategy_params)
            try:
                compare_result = run_strategy_backtest(key, candles, params)
            except (ValueError, TypeError) as exc:
                errors.append(f"{STRATEGIES[key].label}：{exc}")
                continue
            summary = compare_result["summary"]
            highlight = compare_result.get("highlight") or {}
            compare_rows.append(
                {
                    "策略": f"{STRATEGIES[key].icon} {STRATEGIES[key].label}",
                    "总收益率": f"{summary['total_return'] * 100:+.2f}%",
                    "最大回撤": f"{summary['max_drawdown'] * 100:.2f}%",
                    "夏普(年化)": f"{summary['sharpe']:.2f}",
                    "胜率": f"{summary['win_rate'] * 100:.1f}%"
                    if summary.get("closed_trade_count")
                    else "—",
                    "交易笔数": summary["trade_count"],
                    "手续费": f"{summary['total_fees']:,.2f}",
                    "策略亮点": f"{highlight.get('label', '')} {highlight.get('value', '')}".strip(),
                }
            )
            compare_curves[key] = compare_result["equity_curve"]

        if errors:
            for message in errors:
                st.error(message, icon="🚨")
        st.session_state["compare"] = {
            "rows": compare_rows,
            "curves": compare_curves,
            "capital": float(capital),
            "fingerprint": compare_fp,
        }

    compare = st.session_state.get("compare")
    if compare and compare.get("rows"):
        if compare.get("fingerprint") != compare_fp:
            st.warning("行情或资金参数已变化，下面的对比结果是旧数据。", icon="⚠️")
        show_table(pd.DataFrame(compare["rows"]))
        plotly_chart(build_compare_figure(compare["curves"], compare["capital"]))

    st.divider()
    st.caption(
        "⚠️ 风险提示：网格在单边下跌中持续逆势加仓；马丁格尔靠「不止损 + 加仓摊平」换高胜率，"
        "一次单边行情可能吞掉全部利润；趋势策略在震荡市会被反复打止损。"
        "本系统所有撮合与盈亏均为内存中的模拟数字，未连接任何真实资金或下单接口。"
    )


with tab_mood:
    st.markdown("##### 🧠 市场情绪（FOMO 指数）")
    st.caption(
        "恐惧贪婪指数 + 合约资金费 + 多空账户比 的合成分数。只做展示与教学，不参与自动交易。"
    )

    if offline:
        st.info("离线模式不访问后端：情绪与新闻需要在线数据源。", icon="🛰️")
    else:
        sentiment = fetch_sentiment(backend_url, symbol)
        if not sentiment:
            sentiment = fetch_direct_sentiment(symbol)
            if sentiment:
                st.caption("直连模式：后端未部署，情绪数据由前端进程直接获取。")
        if not sentiment:
            st.warning("后端与直连数据源都不可达，情绪暂不可用（行情与回测不受影响）。", icon="📡")
        else:
            if sentiment.get("offline"):
                st.warning(
                    f"外部数据源不可达，当前为兜底数据：{sentiment.get('source', '未知')}", icon="🛰️"
                )
            elif sentiment.get("partial"):
                st.caption(f"部分指标使用默认值：{sentiment.get('source', '未知')}")

            left, right = st.columns([1.15, 1])
            with left:
                plotly_chart(
                    build_sentiment_gauge(float(sentiment["score"]), str(sentiment["label"]))
                )
            with right:
                components = sentiment.get("components") or {}
                fear_greed = components.get("fear_greed")
                funding = components.get("funding_rate")
                ratio = components.get("long_short_ratio")
                metric_left, metric_mid, metric_right = st.columns(3)
                metric_left.metric("恐惧贪婪", "—" if fear_greed is None else f"{fear_greed:.0f}")
                metric_mid.metric("资金费 / 8h", "—" if funding is None else f"{funding * 100:.4f}%")
                metric_right.metric("多空比", "—" if ratio is None else f"{ratio:.2f}")
                st.info(sentiment.get("advice", ""), icon="🧭")
                st.caption(
                    f"来源：{sentiment.get('source', '未知')} · 更新：{sentiment.get('updated_at', '—')}"
                )
                if sentiment.get("label") in ("贪婪", "极度贪婪"):
                    st.caption(
                        "情绪 × 做空：高 FOMO 时空头可收取资金费，但逼空风险最高——"
                        "先等均线转空信号，不要凭情绪裸空。"
                    )

            history = sentiment.get("history") or []
            if history:
                st.markdown("##### 😱 恐惧贪婪指数 · 近 30 天")
                plotly_chart(build_sentiment_history(history))

            st.divider()
            st.markdown("##### 📰 主要新闻")
            news = fetch_news(backend_url)
            if not news or not news.get("items"):
                news = fetch_direct_news()
            if not news or not news.get("items"):
                st.info("新闻源暂不可用。", icon="📰")
            else:
                for item in news["items"]:
                    title = str(item.get("title", "")).replace("[", "(").replace("]", ")")
                    meta = f"{item.get('source', '')} · {item.get('published_at') or '时间未知'}"
                    st.markdown(f"- [{title}]({item.get('link', '#')}) — *{meta}*")
                st.caption(
                    f"来源：{news.get('source', '未知')} · 更新：{news.get('updated_at', '—')}"
                )
                if news.get("offline"):
                    st.caption("⚠️ 当前为离线示例/缓存内容，仅用于演示页面结构。")
