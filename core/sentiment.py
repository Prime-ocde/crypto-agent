"""市场情绪打分（纯函数，方便单测）。

综合三个公开指标：
1. **恐惧贪婪指数**（Alternative.me，0~100）—— 主分量；
2. **合约资金费率**（每 8 小时）—— 为正说明多头拥挤、愿意付费持仓；
3. **多空账户比**（>1 说明多头账户更多）。

输出 0~100 的「FOMO 分数」+ 中文标签 + 一句话解读。
分数只用于展示与教学，不参与自动交易。
"""

from __future__ import annotations

from typing import Any

NEUTRAL_SCORE = 50.0
FUNDING_REFERENCE = 0.0005  # 0.05% / 8h 视为"明显偏热"
FUNDING_WEIGHT = 15.0
LONG_SHORT_REFERENCE = 1.0
LONG_SHORT_WEIGHT = 10.0


def classify(score: float) -> str:
    if score < 25:
        return "极度恐惧"
    if score < 45:
        return "恐惧"
    if score < 55:
        return "中性"
    if score < 75:
        return "贪婪"
    return "极度贪婪"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def composite_sentiment(
    fear_greed: float | None = None,
    funding_rate: float | None = None,
    long_short_ratio: float | None = None,
) -> dict[str, Any]:
    """把三个指标合成一个 0~100 的 FOMO 分数。缺失的指标按中性处理。"""
    base = NEUTRAL_SCORE if fear_greed is None else _clamp(float(fear_greed), 0.0, 100.0)

    funding_adjustment = 0.0
    if funding_rate is not None:
        funding_adjustment = (
            _clamp(float(funding_rate) / FUNDING_REFERENCE, -1.0, 1.0) * FUNDING_WEIGHT
        )

    long_short_adjustment = 0.0
    if long_short_ratio is not None:
        long_short_adjustment = (
            _clamp((float(long_short_ratio) - LONG_SHORT_REFERENCE) / 1.0, -1.0, 1.0)
            * LONG_SHORT_WEIGHT
        )

    score = _clamp(base + funding_adjustment + long_short_adjustment, 0.0, 100.0)

    if score >= 75:
        advice = (
            "FOMO 偏热：多头拥挤、资金费为正，做空可以收到资金费，但逼空风险最高——"
            "别裸空重仓，优先等均线转空信号再动手。"
        )
    elif score <= 25:
        advice = (
            "极度恐惧：空头同样拥挤，追空容易吃反弹；"
            "现货网格可以分批接，合约做空要收紧止损。"
        )
    else:
        advice = "情绪中性：方向性信号弱，回到策略本身的时间尺度与成本（手续费 + 资金费）。"

    return {
        "score": round(score, 1),
        "label": classify(score),
        "advice": advice,
        "components": {
            "fear_greed": None if fear_greed is None else round(float(fear_greed), 1),
            "funding_rate": None if funding_rate is None else float(funding_rate),
            "long_short_ratio": None if long_short_ratio is None else float(long_short_ratio),
        },
        "adjustments": {
            "fear_greed": round(base, 1),
            "funding": round(funding_adjustment, 1),
            "long_short": round(long_short_adjustment, 1),
        },
    }
