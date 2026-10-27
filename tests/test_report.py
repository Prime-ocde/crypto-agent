"""绩效汇总契约单测：净值恒等式、波动率拖累标记。"""

from __future__ import annotations

import pytest

from core.report import build_summary


def _curve(equities: list[float], start_ms: int = 1_700_000_000_000) -> list[dict]:
    return [
        {"t": start_ms + index * 60_000, "time": str(index), "equity": float(value), "price": 100.0}
        for index, value in enumerate(equities)
    ]


def test_summary_uses_last_equity_and_shared_keys() -> None:
    summary = build_summary(
        initial_capital=10_000.0,
        equity_curve=_curve([10_000.0, 10_500.0, 9_800.0]),
        trades=[],
        interval="1w",
        open_position_qty=0.0,
        open_position_value=0.0,
        total_fees=0.0,
        realized_pnl=0.0,
    )
    assert summary["final_equity"] == pytest.approx(9_800.0)
    assert summary["total_return"] == pytest.approx(-0.02)
    assert summary["sample_bars"] == 3
    assert summary["volatility_drag"] is False
    assert summary["trade_count"] == 0


def test_flags_volatility_drag_when_arithmetic_mean_is_positive_but_equity_is_wiped() -> None:
    """+100% / −99.5% / +100%：算术均值正、夏普正，但净值只剩 2%。"""
    summary = build_summary(
        initial_capital=10_000.0,
        equity_curve=_curve([10_000.0, 20_000.0, 100.0, 200.0]),
        trades=[],
        interval="1w",
        open_position_qty=0.0,
        open_position_value=0.0,
        total_fees=0.0,
        realized_pnl=0.0,
    )
    assert summary["total_return"] < -0.5
    assert summary["sharpe"] > 0
    assert summary["volatility_drag"] is True
