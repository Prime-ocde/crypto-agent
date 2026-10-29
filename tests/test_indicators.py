"""技术指标单测：口径必须和 TradingView / 同花顺对得上，边界不能崩。"""

from __future__ import annotations

import math

import pytest

from core.indicators import bollinger, ema, kdj, macd, rsi, sma

LINEAR = [1.0, 2.0, 3.0, 4.0, 5.0]
CONST = [10.0] * 30


def test_sma_alignment_and_warmup() -> None:
    assert sma(LINEAR, 3) == [None, None, 2.0, 3.0, 4.0]
    assert sma([1.0, 2.0], 5) == [None, None]


def test_sma_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        sma(LINEAR, 0)


def test_ema_seeds_with_sma_then_recurses() -> None:
    out = ema(LINEAR, 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)  # (1+2+3)/3
    alpha = 2 / (3 + 1)
    assert out[3] == pytest.approx(alpha * 4 + (1 - alpha) * 2.0)


def test_bollinger_constant_series_has_zero_width() -> None:
    bands = bollinger(CONST, period=20, mult=2.0)
    assert bands["mid"][-1] == pytest.approx(10.0)
    assert bands["upper"][-1] == pytest.approx(10.0)
    assert bands["lower"][-1] == pytest.approx(10.0)
    assert bands["mid"][18] is None  # 预热期


def test_bollinger_band_width_matches_population_std() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    bands = bollinger(values, period=5, mult=2.0)
    mean = 3.0
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / 5)
    assert bands["upper"][-1] == pytest.approx(mean + 2 * std)
    assert bands["lower"][-1] == pytest.approx(mean - 2 * std)


def test_macd_positive_on_uptrend_and_hist_matches_definition() -> None:
    values = [100.0 + index for index in range(80)]
    result = macd(values, 12, 26, 9)
    assert result["dif"][-1] is not None and result["dif"][-1] > 0
    assert result["hist"][-1] == pytest.approx(2 * (result["dif"][-1] - result["dea"][-1]))
    assert result["dif"][:25] == [None] * 25  # 慢线没出来之前没有 DIF


def test_macd_requires_fast_slower_than_slow(monkeypatch) -> None:
    with pytest.raises(ValueError):
        macd(LINEAR, 26, 12, 9)


def test_rsi_boundaries() -> None:
    assert rsi([float(i) for i in range(1, 40)], 14)[-1] == pytest.approx(100.0)
    assert rsi([float(i) for i in range(40, 1, -1)], 14)[-1] == pytest.approx(0.0)
    assert rsi(CONST, 14)[-1] == pytest.approx(50.0)
    assert len(rsi(CONST, 14)) == len(CONST)


def test_rsi_warmup_is_none() -> None:
    out = rsi([float(i) for i in range(1, 30)], 14)
    assert out[:14] == [None] * 14
    assert out[14] is not None


def test_kdj_constant_market_is_neutral() -> None:
    highs = [11.0] * 20
    lows = [9.0] * 20
    closes = [10.0] * 20
    result = kdj(highs, lows, closes, 9, 3, 3)
    assert result["k"][-1] == pytest.approx(50.0)
    assert result["d"][-1] == pytest.approx(50.0)
    assert result["j"][-1] == pytest.approx(50.0)


def test_kdj_flat_bar_uses_rsv_50() -> None:
    result = kdj([10.0] * 12, [10.0] * 12, [10.0] * 12, 9, 3, 3)
    assert result["k"][-1] == pytest.approx(50.0)


def test_kdj_requires_equal_lengths() -> None:
    with pytest.raises(ValueError):
        kdj([1.0, 2.0], [1.0], [1.0, 2.0])


def test_all_indicators_keep_length() -> None:
    values = [100.0 + (index % 7) for index in range(50)]
    for series in (sma(values, 5), ema(values, 5), rsi(values, 14)):
        assert len(series) == len(values)
    assert len(bollinger(values, 20)["mid"]) == len(values)
    assert len(macd(values)["hist"]) == len(values)
