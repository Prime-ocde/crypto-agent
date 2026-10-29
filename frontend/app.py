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
from core.manual_trading import ManualAccount, now_ms  # noqa: E402
from core.indicators import bollinger, kdj, macd, rsi, sma  # noqa: E402
from core.news import merge_news, parse_rss  # noqa: E402
from core.sentiment import composite_sentiment  # noqa: E402
from core.strategy_registry import STRATEGIES, STRATEGY_KEYS  # noqa: E402
from core.strategy_registry import run_backtest as run_strategy_backtest  # noqa: E402
from core.synthetic import default_base_price, synthesize_candles  # noqa: E402
from core.timeframe import interval_label, interval_ms  # noqa: E402
from frontend.charts import render_candles as render_lwc_candles  # noqa: E402

DATA_DIR = ROOT / "data"
DEFAULT_BACKEND = "http://127.0.0.1:8000"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
# 后端不可达时的周期兜底：必须覆盖 README 承诺的周期，否则 DEFAULT_INTERVAL=1w 会被静默丢弃
DEFAULT_INTERVALS = ["1m", "5m", "1h", "1d", "1w", "1M", "1y"]

PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": True}

# 行情降级参数
# 直连超时必须短：实时图 2 秒刷新一次，连接最长 6 秒会把刷新队列拖死
DIRECT_TIMEOUT = (2.0, 4.0)
# 直连失败后的负缓存：30 秒内不再重试（否则每次刷新都在等超时）
DIRECT_RETRY_S = 30.0
# 本地缓存落后超过约 10 根 K 线就算过期 → 改用明确标注的合成行情
CACHE_MAX_BARS = 10

SOURCE_COLORS = {
    "🟢 后端实时": "#26a69a",
    "🟡 后端数据滞后": "#ffb74d",
    "🟡 本地缓存": "#ffb74d",
    "🔴 离线模拟": "#ef5350",
}
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
    return fetch_direct_klines_raw(symbol, interval, limit, timeout=DIRECT_TIMEOUT)


def fetch_direct_klines_guarded(symbol: str, interval: str, limit: int) -> list[dict]:
    """带负缓存的直连：失败后 30 秒内不再尝试。

    没有这层负缓存时，网络差的环境里每 5 秒就会发起一次最长 6 秒的连接，
    而实时图是 2 秒刷新一次 —— 结果就是页面一直卡在"正在运行"，看起来像刷不出行情。
    """
    if st.session_state.get("_direct_down_until", 0.0) > time.time():
        return []
    candles = fetch_direct_klines(symbol, interval, limit)
    if candles:
        st.session_state["_direct_down_until"] = 0.0
    else:
        st.session_state["_direct_down_until"] = time.time() + DIRECT_RETRY_S
    return candles


def _cache_max_age_s(interval: str) -> float:
    """缓存新鲜度阈值：落后超过 ~10 根 K 线就算过期。

    1m 缓存超过 10 分钟、1d 缓存超过 10 天，画出来都是"冻住的历史"，
    宁可换成明确标注的合成行情，也不假装它在实时更新。
    """
    try:
        step = interval_ms(interval) / 1000.0
    except Exception:  # noqa: BLE001
        step = 60.0
    return max(step * CACHE_MAX_BARS, 600.0)


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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
            # 后端 HTTP 活着不等于数据活着：WS 断线时后端会返回几小时前的缓存，
            # 必须按 age_s 判断，否则页面会一直显示"后端实时"却画着冻结的行情。
            age = payload.get("age_s")
            step_s = interval_ms(interval) / 1000.0
            lagging = bool(payload.get("stale")) or (age is not None and age > max(step_s * 2, 60.0))
            if lagging:
                payload["ui_source"] = "🟡 后端数据滞后"
                payload["ui_note"] = (
                    f"后端行情已 {_fmt_age(age)} 未更新（WebSocket 断线，后端正在用 REST 补数据并重连）"
                )
            else:
                payload["ui_source"] = "🟢 后端实时"
                payload["ui_note"] = None
            return payload
        except Exception as exc:  # noqa: BLE001
            st.session_state["_backend_down_until"] = now + 60.0
            note = f"后端不可达（{type(exc).__name__}），已切换到 Binance 直连"

    if not offline:
        direct = fetch_direct_klines_guarded(symbol, interval, limit)
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
        if st.session_state.get("_direct_down_until", 0.0) > now:
            note = f"{note}；直连不可达（{DIRECT_RETRY_S:.0f} 秒内不再重试）"

    candles = load_local_candles(symbol, interval, limit)
    cache_age_s: float | None = None
    if candles:
        cache_age_s = max((time.time() * 1000 - candles[-1]["t"]) / 1000.0, 0.0)
    if candles and cache_age_s is not None and cache_age_s > _cache_max_age_s(interval):
        # 缓存过期：宁可明确标注的合成行情，也不画一根 28 天前冻住的 1m K 线
        stale_age = _fmt_age(cache_age_s)
        candles = synthesize_candles(symbol, interval, limit)
        ui_source = "🔴 离线模拟"
        note = (
            f"{note}；本地缓存已过期 {stale_age}，改用合成行情（非真实价格）"
            if note
            else f"本地缓存已过期 {stale_age}，改用合成行情（非真实价格）"
        )
        return {
            "symbol": symbol,
            "interval": interval,
            "price": float(candles[-1]["c"]),
            "candles": candles,
            "count": len(candles),
            "stale": True,
            "age_s": 0.0,
            "cache_age_s": cache_age_s,
            "source": ui_source,
            "ui_source": ui_source,
            "ui_note": note,
        }

    if candles:
        ui_source = "🟡 本地缓存"
        updated = datetime.fromtimestamp(candles[-1]["t"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        age_text = _fmt_age(cache_age_s)
        note = f"{note}；缓存数据截止 {updated}（{age_text}前）" if note else f"缓存数据截止 {updated}（{age_text}前）"
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
        "age_s": cache_age_s,
        "cache_age_s": cache_age_s,
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



def render_trade_chart(
    candles: list[dict],
    result: dict,
    symbol: str,
    interval: str,
    indicators: dict | None = None,
) -> None:
    """K 线 + 参考线 + 每笔成交点 + 技术指标（与实时图共用同一套指标口径）。"""
    overlays = result.get("overlays") or {}
    levels = overlays.get("grid_levels") or (result.get("grid") or {}).get("levels", [])
    lines = overlays.get("lines") or []
    overlay_lines, panes = _indicator_specs(candles, indicators or {})
    trades = result.get("trades") or []
    signature = (
        symbol,
        interval,
        len(candles),
        len(trades),
        float((result.get("summary") or {}).get("total_return", 0.0) or 0.0),
        tuple(sorted((indicators or {}).get("selected") or [])),
        tuple((indicators or {}).get("ma_periods") or []),
    )
    render_lwc_candles(
        candles=candles,
        levels=levels,
        lines=lines,
        markers=trades,
        interval=interval,
        height=470 + 110 * len(panes),
        key=f"trade-{symbol}-{interval}",
        overlay_lines=overlay_lines,
        sub_panes=panes,
        # 回测结果不变时不重建（保住缩放）；重跑回测后立即重画成交点和指标
        force_reinit=_chart_should_reinit(
            f"trade-{symbol}-{interval}", signature, min_interval=0.0
        ),
    )


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
    render_trade_chart(candles, result, symbol, result["params"]["interval"], live_ctx["indicators"])

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

    with st.expander("📐 技术指标", expanded=True):
        indicator_choices = st.multiselect(
            "叠加 / 副图指标",
            ["MA", "BOLL", "MACD", "RSI", "KDJ"],
            default=["MA", "BOLL", "MACD"],
            help="MA/BOLL 叠在 K 线上；MACD/RSI/KDJ 各自占一个副图",
        )
        ma_periods: list[int] = [5, 20, 60]
        boll_period = 20
        boll_mult = 2.0
        macd_fast, macd_slow, macd_signal = 12, 26, 9
        rsi_period = 14
        kdj_period = 9
        if "MA" in indicator_choices:
            raw_periods = st.text_input(
                "MA 周期（逗号分隔）",
                value="5,20,60",
                key="ma_periods",
                help="最多 6 条；数据不足的周期会自动跳过",
            )
            ma_periods = sorted(
                {int(part) for part in raw_periods.replace("，", ",").split(",") if part.strip().isdigit() and 1 < int(part) <= 500}
            )[:6]
        if "BOLL" in indicator_choices:
            boll_col, mult_col = st.columns(2)
            boll_period = int(boll_col.number_input("BOLL 周期", 5, 200, 20, key="boll_period"))
            boll_mult = float(mult_col.number_input("标准差倍数", 0.5, 4.0, 2.0, 0.1, key="boll_mult"))
        if "MACD" in indicator_choices:
            macd_c1, macd_c2, macd_c3 = st.columns(3)
            macd_fast = int(macd_c1.number_input("MACD 快线", 2, 60, 12, key="macd_fast"))
            macd_slow = int(macd_c2.number_input("MACD 慢线", 3, 120, 26, key="macd_slow"))
            macd_signal = int(macd_c3.number_input("MACD 信号", 2, 60, 9, key="macd_signal"))
        if "RSI" in indicator_choices:
            rsi_period = int(st.number_input("RSI 周期", 2, 100, 14, key="rsi_period"))
        if "KDJ" in indicator_choices:
            kdj_period = int(st.number_input("KDJ 周期", 3, 100, 9, key="kdj_period"))

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


INDICATOR_COLORS = ["#4fc3f7", "#ffb74d", "#26a69a", "#ef5350", "#9e9e9e", "#ba68c8"]
# 这版图表组件没有增量更新，只能靠 forceReinit 重建；节流避免每 2 秒重建一次
CHART_REFRESH_S = float(os.getenv("CHART_REFRESH_SECONDS", "5"))


def _chart_should_reinit(scope: str, signature: tuple, min_interval: float | None = None) -> bool:
    """数据变了才重建图表，且默认最多每 CHART_REFRESH_S 秒重建一次。

    手动交易这种"用户点了才变"的场景传 `min_interval=0`：立即重建，不节流。
    """
    gap = CHART_REFRESH_S if min_interval is None else float(min_interval)
    sig_key, ts_key = f"_chart_sig_{scope}", f"_chart_ts_{scope}"
    now = time.time()
    if st.session_state.get(sig_key) == signature:
        return False
    if now - float(st.session_state.get(ts_key, 0.0)) < gap:
        return False
    st.session_state[sig_key] = signature
    st.session_state[ts_key] = now
    return True


def _indicator_specs(candles: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """把 K 线 + 指标配置翻译成图表要的「叠加线 / 副图」两串声明。

    指标计算全在 `core/indicators.py`（纯函数、有单测），这里只做时间轴对齐与配色。
    """
    closes = [float(candle["c"]) for candle in candles]
    times = [int(candle["t"]) for candle in candles]

    def points(series: list[float | None]) -> list[list[float]]:
        return [[ts, value] for ts, value in zip(times, series) if value is not None]

    selected = set(cfg.get("selected") or [])
    overlays: list[dict] = []
    if "MA" in selected:
        for index, period in enumerate(cfg.get("ma_periods") or []):
            series = sma(closes, int(period))
            if any(value is not None for value in series):
                overlays.append(
                    {
                        "name": f"MA{period}",
                        "color": INDICATOR_COLORS[index % len(INDICATOR_COLORS)],
                        "points": points(series),
                    }
                )
    if "BOLL" in selected:
        bands = bollinger(closes, int(cfg.get("boll_period", 20)), float(cfg.get("boll_mult", 2.0)))
        overlays.extend(
            [
                {"name": "BOLL 上轨", "color": "#90caf9", "points": points(bands["upper"]), "width": 1, "dashed": True},
                {"name": "BOLL 中轨", "color": "#64b5f6", "points": points(bands["mid"]), "width": 1},
                {"name": "BOLL 下轨", "color": "#90caf9", "points": points(bands["lower"]), "width": 1, "dashed": True},
            ]
        )

    panes: list[dict] = []
    pane_id = 2  # 0 = K 线，1 = 成交量
    if "MACD" in selected:
        result = macd(
            closes,
            int(cfg.get("macd_fast", 12)),
            int(cfg.get("macd_slow", 26)),
            int(cfg.get("macd_signal", 9)),
        )
        hist_points: list[list[float]] = []
        hist_colors: list[str] = []
        for ts, value in zip(times, result["hist"]):
            if value is None:
                continue
            hist_points.append([ts, value])
            hist_colors.append("rgba(38,166,154,0.55)" if value >= 0 else "rgba(239,83,80,0.55)")
        panes.append(
            {
                "pane": pane_id,
                "label": (
                    f"MACD({cfg.get('macd_fast', 12)},{cfg.get('macd_slow', 26)},"
                    f"{cfg.get('macd_signal', 9)}) · 柱=2×(DIF−DEA)"
                ),
                "hist": {"points": hist_points, "colors": hist_colors},
                "lines": [
                    {"name": "DIF", "color": "#ffb74d", "points": points(result["dif"])},
                    {"name": "DEA", "color": "#4fc3f7", "points": points(result["dea"])},
                ],
            }
        )
        pane_id += 1
    if "RSI" in selected:
        series = rsi(closes, int(cfg.get("rsi_period", 14)))
        panes.append(
            {
                "pane": pane_id,
                "label": f"RSI({cfg.get('rsi_period', 14)})",
                "lines": [{"name": f"RSI{cfg.get('rsi_period', 14)}", "color": "#ba68c8", "points": points(series)}],
            }
        )
        pane_id += 1
    if "KDJ" in selected:
        result = kdj(
            [float(candle["h"]) for candle in candles],
            [float(candle["l"]) for candle in candles],
            closes,
            int(cfg.get("kdj_period", 9)),
        )
        panes.append(
            {
                "pane": pane_id,
                "label": f"KDJ({cfg.get('kdj_period', 9)},3,3)",
                "lines": [
                    {"name": "K", "color": "#ffb74d", "points": points(result["k"])},
                    {"name": "D", "color": "#4fc3f7", "points": points(result["d"])},
                    {"name": "J", "color": "#ef5350", "points": points(result["j"])},
                ],
            }
        )
        pane_id += 1
    return overlays, panes


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
    age = _fmt_age(payload.get("age_s"))

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

    overlays, panes = _indicator_specs(candles, ctx["indicators"])
    height = 520 + 110 * len(panes)
    last = candles[-1]
    signature = (
        ctx["symbol"],
        ctx["interval"],
        len(candles),
        int(last["t"]),
        round(float(last["c"]), 2),
        tuple(sorted(ctx["indicators"].get("selected") or [])),
        tuple(ctx["indicators"].get("ma_periods") or []),
        ctx["indicators"].get("boll_period"),
        ctx["indicators"].get("macd_fast"),
        ctx["indicators"].get("rsi_period"),
        ctx["indicators"].get("kdj_period"),
    )
    force_reinit = _chart_should_reinit(f"live-{ctx['symbol']}-{ctx['interval']}", signature)

    if not render_lwc_candles(
        candles=candles,
        levels=levels,
        lines=lines,
        interval=ctx["interval"],
        height=height,
        # key 必须稳定：LWC 靠它保存 series 配置，否则 2 秒刷新会把缩放弹回默认
        key=f"live-{ctx['symbol']}-{ctx['interval']}-{strategy}",
        overlay_lines=overlays,
        sub_panes=panes,
        force_reinit=force_reinit,
    ):
        st.warning("当前没有可渲染的 K 线数据。", icon="📉")

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


# ---------------------------------------------------------------- 手动交易
MANUAL_KEY = "manual_account"


def get_manual_account(symbol: str, capital: float) -> ManualAccount:
    """按交易对加载/新建账户：同一个交易对刷新页面不丢仓位（存档在 data/manual/）。"""
    account = st.session_state.get(MANUAL_KEY)
    if account is None or account.symbol != str(symbol).upper():
        account = ManualAccount.load_or_create(
            symbol, capital=float(max(capital, 100.0)), leverage=20.0
        )
        st.session_state[MANUAL_KEY] = account
    return account


def _apply_budget_ratio(ratio: float, cash_available: float) -> None:
    """快捷仓位按钮：改保证金输入框的值（回调里改，避免 widget 已实例化后报错）。"""
    amount = max(float(cash_available) * float(ratio), 1.0)
    st.session_state["manual_margin"] = float(round(amount, 2))


def _manual_records_frame(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records[-12:]).iloc[::-1]
    frame["时间"] = pd.to_datetime(frame["t"], unit="ms").dt.strftime("%m-%d %H:%M:%S")
    frame["动作"] = frame["reason"].fillna("")
    frame["方向"] = frame["position"].map({"long": "多", "short": "空"}).fillna("—")
    frame["价格"] = frame["price"].map(lambda value: f"{value:,.2f}")
    frame["数量"] = frame["qty"].map(lambda value: f"{value:,.4f}")
    frame["盈亏"] = frame["pnl"].map(lambda value: "—" if pd.isna(value) else f"{value:+,.2f}")
    return frame[["时间", "动作", "方向", "价格", "数量", "盈亏", "side"]].rename(columns={"side": "买卖"})


def render_manual_body(ctx: dict) -> None:
    payload = resolve_data(
        ctx["base_url"], ctx["symbol"], ctx["interval"], ctx["limit"], ctx["offline"]
    )
    candles = payload["candles"]
    price = float(payload["price"])
    account = get_manual_account(ctx["symbol"], ctx["capital"])

    # 先推进账户（资金费 + 强平），再读快照
    event = account.tick(price, now_ms())
    if event["liquidations"]:
        account.save()
        for record in event["liquidations"]:
            st.toast(f"💥 强平：{record['reason']}", icon="🚨")
    elif abs(event["funding"]) > 1e-9:
        account.save()

    snapshot = account.snapshot(price)
    flat = snapshot["position"] == "flat"

    st.markdown(
        status_pill(f"最新价 {price:,.2f}", BLUE)
        + status_pill(f"{account.leverage:.0f}x", RED)
        + status_pill("模拟盘 · 不接真实资金", GREY)
        + status_pill(f"存档 {account.path.name}", GREY),
        unsafe_allow_html=True,
    )

    order_col, position_col = st.columns([1, 1.45], gap="medium")

    with order_col:
        st.markdown("##### 📝 下单")
        side_label = st.radio(
            "方向",
            ["做多", "做空"],
            horizontal=True,
            index=0,
            key="manual_side",
            help="做空只在合约模式可用；强平价在开仓价上方",
        )
        side = "long" if side_label == "做多" else "short"

        leverage_value = st.slider(
            "手动杠杆倍数",
            1,
            100,
            int(min(max(account.leverage, 1), 100)),
            1,
            key="manual_leverage",
            disabled=not flat,
            help="有持仓时杠杆锁定（强平价按建仓杠杆计算）；1x 无杠杆，100x 反向 0.5% 即爆仓",
        )

        cash_available = max(snapshot["cash"], 0.0)
        default_margin = min(cash_available, max(cash_available * 0.25, 10.0)) if cash_available else 10.0
        margin = st.number_input(
            "保证金 (USDT)",
            min_value=1.0,
            max_value=max(cash_available, 1.0),
            value=float(round(default_margin, 2)),
            step=10.0,
            key="manual_margin",
            help=f"可用现金 {cash_available:,.2f}；开仓名义价值 = 保证金 × 杠杆，手续费按名义价值收",
        )
        q1, q2, q3 = st.columns(3)
        for column, ratio in ((q1, 0.25), (q2, 0.5), (q3, 1.0)):
            column.button(
                f"{int(ratio * 100)}%",
                key=f"manual_budget_{int(ratio * 100)}",
                width="stretch",
                # 必须用回调：脚本运行之后再改 number_input 的 session_state 会抛异常
                on_click=_apply_budget_ratio,
                args=(ratio, cash_available),
            )

        st.caption(
            f"名义价值约 {margin * account.leverage:,.2f} USDT · "
            f"单边手续费约 {margin * account.leverage * account.ledger.config.fee_rate:,.2f} USDT"
        )

        opposite = snapshot["position"] not in ("flat", side)
        if st.button(
            f"🚀 开{side_label}",
            type="primary",
            width="stretch",
            disabled=opposite or not snapshot["can_open"],
            key="manual_open",
        ):
            try:
                account.set_leverage(leverage_value)
                account.open(side, price, now_ms(), margin_usdt=float(margin))
                account.save()
            except ValueError as exc:
                st.error(f"下单被拒绝：{exc}", icon="🚨")
            else:
                st.toast(f"{side_label}开仓成功 · {margin:,.0f} USDT @ {price:,.2f}", icon="✅")
                # 动作后立即重算：本次运行下方持仓卡片就会显示新持仓（不必等 2 秒自动刷新）
                snapshot = account.snapshot(price)
                flat = snapshot["position"] == "flat"
        if opposite:
            st.caption("已有反向持仓：先平仓再反手，避免一个账户同时持有两个方向的仓位。")

        if st.button(
            "🧯 全部平仓",
            width="stretch",
            disabled=flat,
            key="manual_close",
        ):
            account.close(price, now_ms())
            account.save()
            st.toast("已平仓", icon="✅")
            snapshot = account.snapshot(price)
            flat = snapshot["position"] == "flat"

        with st.expander("⚙️ 账户设置", expanded=False):
            st.caption(f"初始资金 {account.ledger.config.initial_capital:,.0f} USDT · 手续费 {account.ledger.config.fee_rate * 100:.3f}% · 维持保证金率 {account.ledger.config.maintenance_margin * 100:.1f}%")
            st.caption(f"账户存档：{account.path}")
            if st.button("🔄 重置账户（清零重开）", width="stretch", key="manual_reset"):
                fresh = ManualAccount.create(
                    ctx["symbol"], capital=float(max(ctx["capital"], 100.0)), leverage=20.0
                )
                fresh.save(account.path)
                st.session_state[MANUAL_KEY] = fresh
                account = fresh
                snapshot = account.snapshot(price)
                flat = True
                st.toast("账户已重置", icon="🔄")

    with position_col:
        st.markdown("##### 📊 持仓")
        metric_a, metric_b = st.columns(2)
        metric_a.metric("账户权益", f"{snapshot['equity']:,.2f} USDT")
        metric_b.metric(
            "未实现盈亏",
            f"{snapshot['unrealized_pnl']:+,.2f}",
            f"{snapshot['unrealized_pct_of_margin'] * 100:+.2f}% / 保证金" if not flat else None,
        )
        metric_c, metric_d = st.columns(2)
        metric_c.metric("保证金占用率", f"{snapshot['risk_ratio'] * 100:.1f}%")
        metric_d.metric(
            "距强平",
            f"{snapshot['liq_distance_pct'] * 100:.2f}%" if snapshot["liq_distance_pct"] is not None else "—",
        )

        if flat:
            st.caption(
                f"空仓 · 可用现金 {snapshot['cash']:,.2f} USDT · 累计手续费 {snapshot['total_fees']:,.2f}"
            )
        else:
            position_text = "多" if snapshot["position"] == "long" else "空"
            st.caption(
                f"{position_text} {snapshot['qty']:,.4f} 个 · 均价 {snapshot['avg_entry']:,.2f} · "
                f"名义 {snapshot['notional']:,.2f} · 强平价 {snapshot['liq_price']:,.2f} · "
                f"占用保证金 {snapshot['locked_margin']:,.2f}"
            )
            st.caption(
                f"已实现盈亏 {snapshot['realized_pnl']:+,.2f} · 累计资金费 {snapshot['funding_paid']:+,.2f} · "
                f"强平次数 {snapshot['liquidation_count']}"
            )

        for item in snapshot["warnings"]:
            level = item.get("level")
            text = item.get("text", "")
            if level == "error":
                st.error(text, icon="🚨")
            elif level == "warning":
                st.warning(text, icon="⚠️")
            else:
                st.info(text, icon="💸")

    price_lines: list[dict] = []
    if snapshot["liq_price"] is not None:
        price_lines.append(
            {"price": snapshot["liq_price"], "color": RED, "title": "强平价", "width": 2}
        )
    if snapshot["position"] in ("long", "short") and snapshot["avg_entry"] > 0:
        price_lines.append(
            {"price": snapshot["avg_entry"], "color": BLUE, "title": "开仓均价", "width": 1}
        )

    manual_signature = (
        len(account.records),
        snapshot["position"],
        round(float(snapshot["liq_price"] or 0.0), 2),
    )
    render_lwc_candles(
        candles=candles,
        levels=[],
        lines=[],
        markers=account.records[-40:],
        interval=ctx["interval"],
        height=430,
        key=f"manual-{ctx['symbol']}-{ctx['interval']}",
        price_lines=price_lines,
        # 手动交易是"点了才变"，不节流：开平仓后立刻重画，才能马上看到成交点和强平线
        force_reinit=_chart_should_reinit(
            f"manual-{ctx['symbol']}-{ctx['interval']}", manual_signature, min_interval=0.0
        ),
    )

    st.markdown("##### 🧾 成交与爆仓记录")
    frame = _manual_records_frame(account.records)
    if frame.empty:
        st.caption("还没有成交。开仓后会在这里看到每一笔的真实盈亏（含双边手续费）。")
    else:
        show_table(frame)


@st.fragment(run_every="2s")
def manual_panel_auto(ctx: dict) -> None:
    render_manual_body(ctx)


def manual_panel(ctx: dict) -> None:
    render_manual_body(ctx)


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
    "capital": float(capital),
    "indicators": {
        "selected": indicator_choices,
        "ma_periods": ma_periods,
        "boll_period": boll_period,
        "boll_mult": boll_mult,
        "macd_fast": macd_fast,
        "macd_slow": macd_slow,
        "macd_signal": macd_signal,
        "rsi_period": rsi_period,
        "kdj_period": kdj_period,
    },
}

tab_live, tab_manual, tab_backtest, tab_mood = st.tabs(
    ["📊 实时行情", "💼 手动交易", "🧮 回测分析", "🧠 情绪 · 新闻"]
)

with tab_live:
    if paused:
        live_panel_manual(live_ctx)
    else:
        live_panel_auto(live_ctx)

with tab_manual:
    if paused:
        manual_panel(live_ctx)
    else:
        manual_panel_auto(live_ctx)

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
