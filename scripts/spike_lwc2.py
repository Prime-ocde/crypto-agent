"""Spike：验证自建 Lightweight Charts 组件能「增量更新且不重置缩放」。

跑法：streamlit run scripts/spike_lwc2.py --server.port 8505

页面每 2 秒更新最后一根 K 线的收盘价（真实行情），但**不会**重建图表。
右上是当前可见区间（from/to），刷新时不应变化。
"""

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.direct_feed import fetch_klines  # noqa: E402
from frontend.lwc import build_payload, render  # noqa: E402

st.set_page_config(layout="wide")
st.markdown("### 增量更新 spike · 缩放应当保持")

if "spike_rows" not in st.session_state:
    st.session_state["spike_rows"] = fetch_klines("BTCUSDT", "1m", 300)
    st.session_state["spike_reset"] = True

rows = st.session_state["spike_rows"]


@st.fragment(run_every="2s")
def live() -> None:
    fresh = fetch_klines("BTCUSDT", "1m", 2)
    rows_now = st.session_state["spike_rows"]
    if fresh and rows_now:
        # 只更新最后一根（模拟增量推送）
        last = dict(rows_now[-1])
        last["c"] = fresh[-1]["c"]
        last["h"] = max(last["h"], fresh[-1]["c"])
        last["l"] = min(last["l"], fresh[-1]["c"])
        rows_now[-1] = last
        st.session_state["spike_rows"] = rows_now
    payload = build_payload(
        st.session_state["spike_rows"],
        height=460,
        price_line={"price": st.session_state["spike_rows"][-1]["c"], "title": "最新"},
        reset_view=bool(st.session_state.get("spike_reset")),
    )
    st.session_state["spike_reset"] = False
    render(payload, key="spike-min", height=460)
    st.caption(
        f"最后收盘 {st.session_state['spike_rows'][-1]['c']:,.2f} · "
        f"调用次数 {st.session_state.get('spike_n', 0)}"
    )
    st.session_state["spike_n"] = st.session_state.get("spike_n", 0) + 1


live()
st.caption("缩放/左右平移后再等几秒：视口应保持不动，只有最后一根在变。")
