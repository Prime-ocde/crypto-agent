"""共享执行账本单测：现货等价、杠杆放大、强平、资金费率。"""

from __future__ import annotations

import pytest

from core.execution import Ledger, TradeConfig

EIGHT_HOURS_MS = 8 * 3600 * 1000


def test_spot_ledger_keeps_original_cash_flow() -> None:
    ledger = Ledger(TradeConfig(mode="spot", leverage=1, fee_rate=0.001, initial_capital=10_000))

    lot = ledger.open_long(100.0, 1, 1000.0, "买入")
    assert lot is not None
    assert lot["qty"] == pytest.approx(10.0)
    assert lot["margin"] == pytest.approx(1000.0)
    assert ledger.cash == pytest.approx(10_000 - 1000 - 1.0)
    assert ledger.equity(100.0) == pytest.approx(9_999.0)

    record = ledger.close_lot(0, 110.0, 2, "卖出")
    assert record is not None
    assert record["pnl"] == pytest.approx(100 - 1.1 - 1.0)
    assert ledger.cash == pytest.approx(10_000 - 1000 - 1.0 + 1000 + 100 - 1.1)
    assert ledger.equity(110.0) == pytest.approx(ledger.cash)


def test_futures_margin_amplifies_pnl() -> None:
    ledger = Ledger(
        TradeConfig(mode="futures", leverage=10, fee_rate=0.0005, initial_capital=10_000)
    )
    lot = ledger.open_long(100.0, 1, 1000.0, "开多")
    assert lot is not None
    # 保证金 1000 × 10 倍 = 名义 10,000 → 100 个币；手续费按名义收
    assert lot["qty"] == pytest.approx(100.0)
    assert lot["margin"] == pytest.approx(1000.0)
    assert ledger.cash == pytest.approx(10_000 - 1000 - 5.0)
    # 价格 +1% → 名义价值 +100（相对于 10% 的保证金收益）
    assert ledger.equity(101.0) == pytest.approx(10_000 - 5.0 + 100.0)

    record = ledger.close_lot(0, 101.0, 2, "平多")
    assert record is not None
    assert record["pnl"] == pytest.approx(100 - 5.05 - 5.0)


def test_liquidation_wipes_margin() -> None:
    ledger = Ledger(
        TradeConfig(
            mode="futures",
            leverage=10,
            fee_rate=0.0,
            initial_capital=1000,
            maintenance_margin=0.005,
        )
    )
    ledger.open_long(100.0, 1, 1000.0, "满仓开多")
    assert ledger.liquidation_price(ledger.lots[0]) == pytest.approx(90.5)

    records = ledger.check_liquidation(90.0, 91.0, 2)
    assert len(records) == 1
    assert records[0]["reason"].startswith("强平")
    assert ledger.liquidation_count == 1
    assert ledger.equity(90.0) == pytest.approx(0.0), "10 倍杠杆满仓，反向 10% 即本金归零"


def test_spot_never_liquidates() -> None:
    ledger = Ledger(TradeConfig(mode="spot", initial_capital=1000))
    ledger.open_long(100.0, 1, 1000.0)
    assert ledger.check_liquidation(1.0, 200.0, 2) == []
    assert ledger.liquidation_count == 0


def test_short_profits_when_price_falls() -> None:
    ledger = Ledger(TradeConfig(mode="futures", leverage=5, fee_rate=0.0, initial_capital=10_000))
    lot = ledger.open_short(100.0, 1, 1000.0)  # 名义 5000 → 50 个币
    assert lot is not None and lot["side"] == "short"
    assert ledger.equity(100.0) == pytest.approx(10_000.0)
    assert ledger.equity(90.0) == pytest.approx(10_500.0)

    record = ledger.close_lot(0, 90.0, 2)
    assert record is not None
    assert record["side"] == "buy" and record["position"] == "short"
    assert record["pnl"] == pytest.approx(500.0)
    assert ledger.cash == pytest.approx(10_500.0)


def test_short_liquidation_on_pump() -> None:
    ledger = Ledger(
        TradeConfig(
            mode="futures",
            leverage=10,
            fee_rate=0.0,
            initial_capital=1000,
            maintenance_margin=0.005,
        )
    )
    ledger.open_short(100.0, 1, 1000.0)
    assert ledger.liquidation_price(ledger.lots[0]) == pytest.approx(109.5)

    records = ledger.check_liquidation(100.0, 110.0, 2)
    assert len(records) == 1
    assert records[0]["position"] == "short"
    assert "空" in records[0]["reason"]
    assert ledger.equity(110.0) == pytest.approx(0.0)


def test_short_receives_funding() -> None:
    ledger = Ledger(
        TradeConfig(
            mode="futures",
            leverage=5,
            fee_rate=0.0,
            funding_rate_8h=0.001,
            initial_capital=10_000,
        )
    )
    ledger.open_short(100.0, 0, 1000.0)  # 名义 5000
    ledger.apply_funding(0, 100.0)

    amount = ledger.apply_funding(EIGHT_HOURS_MS, 100.0)
    assert amount == pytest.approx(-5.0), "空头收取资金费，符号为负（净收入）"
    assert ledger.funding_paid == pytest.approx(-5.0)
    assert ledger.cash == pytest.approx(9_005.0)
    assert ledger.equity(100.0) == pytest.approx(10_005.0)


def test_spot_rejects_short() -> None:
    ledger = Ledger(TradeConfig(mode="spot", initial_capital=1000))
    with pytest.raises(ValueError, match="现货模式不支持做空"):
        ledger.open_short(100.0, 1, 100.0)


def test_funding_charged_every_eight_hours() -> None:
    ledger = Ledger(
        TradeConfig(
            mode="futures",
            leverage=5,
            fee_rate=0.0,
            funding_rate_8h=0.001,
            initial_capital=10_000,
        )
    )
    ledger.open_long(100.0, 0, 1000.0)  # 名义 5000 → 50 个币

    assert ledger.apply_funding(0, 100.0) == 0.0  # 第一次只校准时钟
    assert ledger.apply_funding(EIGHT_HOURS_MS, 100.0) == pytest.approx(5.0)
    assert ledger.apply_funding(3 * EIGHT_HOURS_MS, 100.0) == pytest.approx(10.0)
    assert ledger.funding_paid == pytest.approx(15.0)


def test_funding_not_blocked_by_zero_free_cash() -> None:
    """满仓时可用现金≈0，资金费必须从保证金账户扣，而不是被"现金为 0"吞掉。"""
    ledger = Ledger(
        TradeConfig(
            mode="futures",
            leverage=10,
            fee_rate=0.0,
            funding_rate_8h=0.001,
            initial_capital=1000,
        )
    )
    ledger.open_long(100.0, 0, 1000.0)  # 名义 10,000，现金 0
    ledger.apply_funding(0, 100.0)

    paid = ledger.apply_funding(EIGHT_HOURS_MS, 100.0)
    assert paid == pytest.approx(10.0), "10,000 名义 × 0.1% = 10 USDT"
    assert ledger.cash == pytest.approx(-10.0)
    assert ledger.equity(100.0) == pytest.approx(990.0)


def test_rejects_invalid_execution_config() -> None:
    with pytest.raises(ValueError, match="现货模式不能设置杠杆"):
        TradeConfig(mode="spot", leverage=5).validate()
    with pytest.raises(ValueError, match="交易模式"):
        TradeConfig(mode="margin").validate()
    with pytest.raises(ValueError, match="杠杆倍数"):
        TradeConfig(mode="futures", leverage=0).validate()
    with pytest.raises(ValueError, match="资金费率"):
        TradeConfig(mode="futures", funding_rate_8h=0.05).validate()
