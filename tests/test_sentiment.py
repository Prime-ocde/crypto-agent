"""市场情绪打分单测：标签边界、分量调整、缺失指标、极值钳制。"""

from __future__ import annotations

import pytest

from core.sentiment import classify, composite_sentiment


def test_neutral_when_everything_missing() -> None:
    result = composite_sentiment()
    assert result["score"] == pytest.approx(50.0)
    assert result["label"] == "中性"
    assert result["components"] == {
        "fear_greed": None,
        "funding_rate": None,
        "long_short_ratio": None,
    }


def test_label_boundaries() -> None:
    assert classify(0) == "极度恐惧"
    assert classify(24.9) == "极度恐惧"
    assert classify(25) == "恐惧"
    assert classify(45) == "中性"
    assert classify(55) == "贪婪"
    assert classify(75) == "极度贪婪"


def test_hot_market_clamps_to_one_hundred() -> None:
    result = composite_sentiment(fear_greed=95, funding_rate=0.002, long_short_ratio=3.0)
    assert result["score"] == pytest.approx(100.0)
    assert result["label"] == "极度贪婪"
    assert "逼空" in result["advice"]
    # 分量各自有上限：资金费最多 +15，多空比最多 +10
    assert result["adjustments"]["funding"] == pytest.approx(15.0)
    assert result["adjustments"]["long_short"] == pytest.approx(10.0)


def test_cold_market_clamps_to_zero() -> None:
    result = composite_sentiment(fear_greed=5, funding_rate=-0.002, long_short_ratio=0.2)
    assert result["score"] == pytest.approx(0.0)
    assert result["label"] == "极度恐惧"
    assert "反弹" in result["advice"]


def test_partial_inputs_are_treated_as_neutral_adjustment() -> None:
    only_fear = composite_sentiment(fear_greed=70)
    assert only_fear["score"] == pytest.approx(70.0)
    assert only_fear["adjustments"]["funding"] == pytest.approx(0.0)

    only_funding = composite_sentiment(funding_rate=0.0005)
    assert only_funding["score"] == pytest.approx(65.0)
