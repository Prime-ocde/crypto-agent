"""阶段二 spike：验证 streamlit-lightweight-charts-pro 0.3.0 能否直接替换 Plotly 的 K 线。

用法：
    .venv/Scripts/python.exe scripts/spike_lwc.py --check    # 只校验配置能序列化（不需要浏览器）
    streamlit run scripts/spike_lwc.py                       # 真正打开页面看效果

它验证四件事（这四条决定阶段二是否可行）：
1. 组件是否需要 npm 构建 —— wheel 自带 frontend/build，生产模式直接加载静态资源；
2. 毫秒时间戳必须转成 UNIX 秒，否则时间轴错到几万年以后；
3. 虚线价格线（网格/强平线）、逐点染色成交量、markers（成交点）是否都被支持；
4. 2 秒一次的 rerun 会不会把缩放弹回默认（key 稳定时组件内部保存 series 配置）。

注意：`--check` 只做 Python 侧序列化，不代表前端真的画出来了；视觉验收必须 `streamlit run`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.direct_feed import fetch_klines  # noqa: E402
from core.synthetic import synthesize_candles  # noqa: E402
from frontend.charts import build_chart, clean_rows, render_candles  # noqa: E402


def sample_candles(limit: int = 300) -> list[dict]:
    candles = fetch_klines("BTCUSDT", "1d", limit)
    return candles or synthesize_candles("BTCUSDT", "1d", limit)


def check() -> int:
    candles = sample_candles()
    rows = clean_rows(candles)
    print(f"candles: {len(candles)} → clean: {len(rows)}")
    print(f"first: {rows[0]['t']} ({rows[0]['t'] > 10**9}), last: {rows[-1]['t']}")

    chart = build_chart(
        candles=rows,
        levels=[rows[-1]["c"] * 0.98, rows[-1]["c"] * 1.02],
        lines=[{"name": "SMA20", "color": "#4fc3f7", "points": []}],
        markers=[
            {"t": rows[-3]["t"] * 1000, "price": rows[-3]["c"], "side": "buy", "reason": ""},
            {"t": rows[-2]["t"] * 1000, "price": rows[-2]["c"], "side": "sell", "reason": "强平（10x 多）"},
        ],
        interval="1d",
    )
    if chart is None:
        print("FAIL: build_chart returned None")
        return 1

    config = chart.to_frontend_config()
    chart_config = (config.get("charts") or [{}])[0]
    series = chart_config.get("series") or []
    print(f"series: {len(series)} → {[s.get('type') for s in series]}")
    candles_series = series[0]
    print(f"pane ids: {[s.get('paneId') for s in series]}")
    print(f"price_lines: {len(candles_series.get('priceLines') or [])}")
    print(f"markers: {len(candles_series.get('markers') or [])}")
    print(f"chart option keys: {sorted((chart_config.get('chart') or {}).keys())}")
    switched = (chart_config.get("chart") or {}).get("rangeSwitcher") or {}
    print(f"range_switcher: {switched.get('visible')} · {[r.get('text') for r in switched.get('ranges') or []]}")
    print("OK: 配置可序列化")
    return 0


def app() -> None:
    import streamlit as st

    st.set_page_config(page_title="LWC spike", layout="wide")
    st.title("Lightweight Charts spike · BTCUSDT 1d")
    st.caption("只验证渲染与交互，不是正式页面")

    if st.button("重新拉取"):
        st.cache_data.clear()

    candles = sample_candles()
    if not candles:
        st.error("没有行情数据")
        return

    last = candles[-1]["c"]
    render_candles(
        candles=candles,
        levels=[last * 0.97, last * 0.985, last * 1.015, last * 1.03],
        lines=[],
        markers=[
            {"t": candles[-4]["t"], "price": candles[-4]["c"], "side": "buy", "reason": ""},
            {"t": candles[-2]["t"], "price": candles[-2]["c"], "side": "sell", "reason": ""},
        ],
        interval="1d",
        key="spike",
    )
    st.write(f"共 {len(candles)} 根，最新 {last:,.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验配置序列化")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(check())
    app()
