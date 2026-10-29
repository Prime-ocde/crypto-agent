"""前端冒烟测试：用 Streamlit 官方 AppTest 真跑一遍脚本。

覆盖：后端不可达时的三级降级（合成行情）不会让页面崩，
      并且侧边栏、K 线图、回测按钮都成功渲染。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = Path(__file__).resolve().parents[1] / "frontend" / "app.py"


def test_app_renders_without_backend() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()

    assert not app.exception, f"页面渲染抛异常：{app.exception}"
    assert any("量化策略" in title.value for title in app.title)

    labels = [selectbox.label for selectbox in app.selectbox]
    assert "交易对" in labels
    assert "K 线周期" in labels
    assert "策略" in labels

    button_labels = [button.label for button in app.button]
    assert any("运行回测" in label for label in button_labels)


def test_backtest_button_produces_metrics() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()
    assert not app.exception

    run_button = next(button for button in app.button if "运行回测" in button.label)
    run_button.click().run()

    assert not app.exception, f"回测按钮抛异常：{app.exception}"
    metric_labels = [metric.label for metric in app.metric]
    assert "总收益率" in metric_labels
    assert "最大回撤" in metric_labels
    assert "夏普比率" in metric_labels
    assert "胜率" in metric_labels


def test_reset_bounds_button_restores_default_range() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()
    assert not app.exception

    lower_box = next(box for box in app.number_input if "下界" in box.label)
    default_lower = float(lower_box.value)

    lower_box.set_value(default_lower * 0.9).run()
    assert not app.exception
    assert float(next(box for box in app.number_input if "下界" in box.label).value) < default_lower

    reset_button = next(button for button in app.button if "以最新价重置" in button.label)
    reset_button.click().run()

    assert not app.exception, f"重置按钮抛异常：{app.exception}"
    restored = float(next(box for box in app.number_input if "下界" in box.label).value)
    assert abs(restored - default_lower) <= max(1.0, default_lower * 0.01)


def test_pause_toggle_renders_without_timer_fragment() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()
    assert not app.exception

    pause = next(toggle for toggle in app.toggle if "暂停" in toggle.label)
    pause.set_value(True).run()

    assert not app.exception, f"暂停刷新后渲染异常：{app.exception}"


def test_switch_to_trend_strategy_renders_its_params_and_backtests() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()
    assert not app.exception

    strategy_box = next(box for box in app.selectbox if box.label == "策略")
    strategy_box.set_value("trend").run()
    assert not app.exception, f"切换到趋势策略抛异常：{app.exception}"

    labels = [box.label for box in app.number_input]
    assert "快线周期" in labels
    assert "慢线周期" in labels

    run_button = next(button for button in app.button if "运行回测" in button.label)
    run_button.click().run()

    assert not app.exception, f"趋势策略回测抛异常：{app.exception}"
    metric_labels = [metric.label for metric in app.metric]
    assert "总收益率" in metric_labels
    assert "最大回撤" in metric_labels


def test_switch_to_martingale_renders_its_params() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()

    strategy_box = next(box for box in app.selectbox if box.label == "策略")
    strategy_box.set_value("martingale").run()

    assert not app.exception, f"切换到马丁格尔抛异常：{app.exception}"
    labels = [box.label for box in app.number_input]
    assert "首次建仓 (USDT)" in labels
    assert "加仓倍数" in labels


def test_futures_mode_exposes_leverage_control() -> None:
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()
    assert not app.exception

    mode_radio = next(radio for radio in app.radio if radio.label == "市场模式")
    mode_radio.set_value("futures").run()

    assert not app.exception, f"切换到合约模式抛异常：{app.exception}"
    slider_labels = [slider.label for slider in app.slider]
    assert "杠杆倍数" in slider_labels

    run_button = next(button for button in app.button if "运行回测" in button.label)
    run_button.click().run()
    assert not app.exception, f"合约模式回测抛异常：{app.exception}"


def test_weekly_interval_renders(monkeypatch) -> None:
    """周 K 的 rangeselector 不能用 Plotly 的 step='week'（会直接抛 ValueError）。"""
    monkeypatch.setenv("DEFAULT_INTERVAL", "1w")
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()

    assert not app.exception, f"周 K 渲染抛异常：{app.exception}"
    assert app.selectbox  # 页面结构仍然正常


def test_backtest_uses_selected_interval(monkeypatch) -> None:
    """回测必须把当前周期传给引擎，否则年化系数会用错（1m vs 1w 差 1 万倍）。"""
    monkeypatch.setenv("DEFAULT_INTERVAL", "1w")
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()

    run_button = next(button for button in app.button if "运行回测" in button.label)
    run_button.click().run()

    assert not app.exception, f"周 K 回测抛异常：{app.exception}"
    captions = [caption.value for caption in app.caption]
    assert any("周期：1 周" in caption for caption in captions), captions[:4]


def test_offline_mode_renders_candles_even_when_cache_is_stale() -> None:
    """降级到离线时图表必须有数据：1m 缓存过期曾导致页面只剩一根都画不出来。"""
    app = AppTest.from_file(str(APP_PATH), default_timeout=90)
    app.run()

    offline = next(toggle for toggle in app.toggle if "离线" in toggle.label)
    offline.set_value(True).run()
    assert not app.exception, f"离线模式抛异常：{app.exception}"

    components = app.get("component_instance")
    assert components, "离线模式没有渲染任何图表组件"
    config = json.loads(components[0].proto.json_args)["config"]
    series = (config.get("charts") or [{}])[0].get("series") or []
    assert series, "图表组件没有 series"
    assert len(series[0].get("data") or []) > 0, "离线降级后 K 线数据为空"


def test_manual_tab_opens_and_closes_position(tmp_path, monkeypatch) -> None:
    """手动交易闭环：100x 开多 → 出现强平预警 → 平仓 → 账户落盘。"""
    monkeypatch.setattr("core.manual_trading.DEFAULT_DATA_DIR", tmp_path)

    app = AppTest.from_file(str(APP_PATH), default_timeout=120)
    app.run()
    assert not app.exception, f"页面渲染抛异常：{app.exception}"
    assert any(tab.label == "💼 手动交易" for tab in app.tabs)

    leverage = next(slider for slider in app.slider if slider.label == "手动杠杆倍数")
    leverage.set_value(100).run()

    open_button = next(button for button in app.button if "开做多" in button.label)
    open_button.click().run()
    assert not app.exception, f"开仓抛异常：{app.exception}"

    metrics = {metric.label: metric.value for metric in app.metric}
    assert "账户权益" in metrics
    assert metrics["距强平"] == "0.50%", "100x 满仓做多，距强平应约 0.5%"

    close_button = next(button for button in app.button if "全部平仓" in button.label)
    close_button.click().run()
    assert not app.exception, f"平仓抛异常：{app.exception}"
    assert {metric.label: metric.value for metric in app.metric}["距强平"] == "—"
    assert (tmp_path / "BTCUSDT.json").exists(), "平仓后账户应已落盘"
