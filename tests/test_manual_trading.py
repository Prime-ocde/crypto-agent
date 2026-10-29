"""手动交易账户单测：100x 杠杆、建仓锁杠杆、资金费、强平预警、落盘恢复。

这些测试锁死的是「模拟盘不能骗人」的几条口径：
- 强平后该笔保证金+手续费全部损失，权益不会出现负数；
- 持仓期间改杠杆不能追溯改写旧仓位的强平价；
- 落盘再读回来，现金/持仓/成交记录必须逐字节一致。
"""

from __future__ import annotations

import json

import pytest

from core.execution import Ledger, TradeConfig
from core.manual_trading import ManualAccount, default_path

HOURS_8 = 8 * 3600 * 1000


def make_account(capital: float = 10_000.0, leverage: float = 10.0, **kwargs) -> ManualAccount:
    return ManualAccount.create("BTCUSDT", capital=capital, leverage=leverage, **kwargs)


# ---------------------------------------------------------------- 参数与杠杆
def test_leverage_accepts_100x_and_rejects_101x() -> None:
    account = make_account(leverage=100)
    assert account.leverage == 100.0
    with pytest.raises(ValueError, match="杠杆倍数"):
        TradeConfig(mode="futures", leverage=101).validate()


def test_spot_account_forces_leverage_one_and_rejects_short() -> None:
    account = ManualAccount.create("BTCUSDT", capital=1000, leverage=20, mode="spot")
    assert account.leverage == 1.0
    assert account.ledger.config.funding_rate_8h == 0.0
    with pytest.raises(ValueError, match="现货模式不支持做空"):
        account.open("short", 100.0, 0, margin_usdt=100)


def test_leverage_is_locked_per_position() -> None:
    """建仓后把配置杠杆改成 50x，旧仓位的强平价必须还是按 10x 算。"""
    ledger = Ledger(TradeConfig(mode="futures", leverage=10, fee_rate=0.0, initial_capital=10_000))
    lot = ledger.open_long(100.0, 0, 1000.0)
    assert lot is not None
    before = ledger.liquidation_price(lot)

    ledger.config = TradeConfig(mode="futures", leverage=50, fee_rate=0.0, initial_capital=10_000)
    assert ledger.liquidation_price(lot) == pytest.approx(before)
    assert ledger.liquidation_price(lot) == pytest.approx(100 * (1 - 0.1 + 0.005))


def test_cannot_change_leverage_while_holding() -> None:
    account = make_account()
    account.open("long", 100.0, 0, margin_usdt=1000)
    with pytest.raises(ValueError, match="有持仓时不能改杠杆"):
        account.set_leverage(20)
    account.close(100.0, 1)
    account.set_leverage(20)
    assert account.leverage == 20.0


# ---------------------------------------------------------------- 开平仓
def test_open_long_moves_cash_and_records_trade() -> None:
    account = make_account()
    lot = account.open("long", 100.0, 1, margin_usdt=1000)
    assert lot is not None
    assert lot["qty"] == pytest.approx(100.0)  # 1000 × 10 倍 / 100
    assert account.ledger.cash == pytest.approx(10_000 - 1000 - 5.0)
    assert len(account.records) == 1
    assert account.records[0]["reason"] == "手动开仓"

    snapshot = account.snapshot(100.0)
    assert snapshot["position"] == "long"
    assert snapshot["locked_margin"] == pytest.approx(1000.0)
    assert snapshot["equity"] == pytest.approx(9_995.0)


def test_close_all_realizes_pnl_and_flattens() -> None:
    account = make_account()
    account.open("long", 100.0, 1, margin_usdt=1000)
    records = account.close(110.0, 2)
    assert len(records) == 1
    assert records[0]["pnl"] == pytest.approx(1000 - 5.5 - 5.0)
    snapshot = account.snapshot(110.0)
    assert snapshot["position"] == "flat"
    assert snapshot["locked_margin"] == 0.0
    assert len(account.records) == 2


def test_margin_budget_is_clamped_to_available_cash() -> None:
    account = make_account(capital=1000)
    lot = account.open("long", 100.0, 0, margin_usdt=10_000)
    assert lot is not None
    assert lot["scaled"] is True
    assert account.ledger.cash >= 0
    assert lot["margin"] <= 1000


def test_rejects_invalid_direction_and_zero_price() -> None:
    account = make_account()
    with pytest.raises(ValueError, match="方向必须是"):
        account.open("up", 100.0, 0, margin_usdt=100)
    with pytest.raises(ValueError, match="价格必须大于 0"):
        account.open("long", 0.0, 0, margin_usdt=100)


# ---------------------------------------------------------------- 资金费与强平
def test_long_pays_funding_every_eight_hours() -> None:
    account = make_account()
    account.open("long", 100.0, 0, margin_usdt=1000)  # 名义 10,000
    account.tick(100.0, 0)  # 播种资金费时钟
    result = account.tick(100.0, HOURS_8)
    assert result["funding"] == pytest.approx(1.0)
    assert result["liquidations"] == []
    assert account.ledger.funding_paid == pytest.approx(1.0)


def test_100x_liquidation_wipes_margin_and_blocks_new_orders() -> None:
    account = make_account(capital=1000, leverage=100)
    lot = account.open("long", 100.0, 0, margin_usdt=1000)  # 满仓，强平价 99.5
    assert lot is not None
    assert account.snapshot(100.0)["liq_price"] == pytest.approx(99.5)

    result = account.tick(99.4, 1)
    assert len(result["liquidations"]) == 1
    assert result["liquidations"][0]["reason"].startswith("强平")
    assert "100x" in result["liquidations"][0]["reason"]
    assert account.ledger.equity(99.4) == pytest.approx(0.0)
    assert len(account.liquidations) == 1
    assert account.snapshot(99.4)["can_open"] is False
    with pytest.raises(ValueError, match="权益已归零"):
        account.open("long", 99.4, 2, margin_usdt=10)


def test_liquidation_warning_levels() -> None:
    account = make_account(capital=10_000, leverage=20)
    account.open("long", 100.0, 0, margin_usdt=1000)  # 强平价 95.5
    near = account.snapshot(95.9)  # 距强平约 0.4%
    assert near["position"] == "long"
    assert any(item["level"] == "error" for item in near["warnings"])

    safe = account.snapshot(120.0)
    assert [item["level"] for item in safe["warnings"]] == ["info"]  # 只剩资金费提示


def test_short_receives_funding() -> None:
    account = make_account()
    account.open("short", 100.0, 0, margin_usdt=1000)
    account.tick(100.0, 0)
    result = account.tick(100.0, HOURS_8)
    assert result["funding"] == pytest.approx(-1.0)
    assert account.ledger.cash > 10_000 - 1000 - 5.0


# ---------------------------------------------------------------- 落盘
def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "BTCUSDT.json"
    account = ManualAccount.create("BTCUSDT", capital=5000, leverage=20, path=path)
    account.open("long", 100.0, 0, margin_usdt=500)
    account.tick(101.0, HOURS_8)
    account.save()

    loaded = ManualAccount.load(path, symbol="BTCUSDT")
    assert loaded.to_dict()["ledger"] == account.to_dict()["ledger"]
    assert len(loaded.records) == len(account.records) == 1
    assert loaded.leverage == 20.0
    assert loaded.snapshot(101.0)["equity"] == pytest.approx(account.snapshot(101.0)["equity"])


def test_load_rejects_wrong_symbol_and_bad_json(tmp_path) -> None:
    path = tmp_path / "BTCUSDT.json"
    account = ManualAccount.create("BTCUSDT", path=path)
    account.save()

    with pytest.raises(ValueError, match="不匹配"):
        ManualAccount.load(path, symbol="ETHUSDT")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="无法解析"):
        ManualAccount.load(broken)


def test_load_or_create_recovers_or_rebuilds(tmp_path) -> None:
    path = tmp_path / "acct.json"
    first = ManualAccount.create("BTCUSDT", capital=2000, path=path)
    first.open("short", 100.0, 0, margin_usdt=200)
    first.save()

    restored = ManualAccount.load_or_create("BTCUSDT", path=path)
    assert len(restored.ledger.lots) == 1
    assert restored.ledger.config.initial_capital == 2000

    path.write_text("{}", encoding="utf-8")  # 损坏存档
    rebuilt = ManualAccount.load_or_create("BTCUSDT", path=path, capital=3000)
    assert rebuilt.ledger.lots == []
    assert rebuilt.ledger.config.initial_capital == 3000


def test_default_path_is_per_symbol() -> None:
    assert default_path("btcusdt").name == "BTCUSDT.json"
    assert default_path("BTC/USDT").name == "BTCUSDT.json"


def test_saved_file_is_valid_json_with_schema(tmp_path) -> None:
    path = tmp_path / "acct.json"
    account = ManualAccount.create("BTCUSDT", path=path)
    account.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["symbol"] == "BTCUSDT"
    assert payload["ledger"]["config"]["mode"] == "futures"
