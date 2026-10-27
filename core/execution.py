"""共享执行账本：现货 / 合约（多头 + 空头）同一套记账。

## 设计要点

- **现货 = 杠杆 1 + 无做空**：`margin = notional`、无强平、无资金费率，
  数学上与原实现完全等价；
- **合约双向**：`margin = notional / leverage`，手续费按名义价值收；
  - 多头晕平价：`entry × (1 − 1/leverage + 维持保证金率)`，最低价击穿即爆；
  - 空头晕平价：`entry × (1 + 1/leverage − 维持保证金率)`，最高价击穿即爆；
- **资金费**：按**净头寸**名义价值每 8 小时结算，多头支付、空头收取；
- **权益**：`equity = 可用现金 + 占用保证金 + Σ 方向相关的未实现盈亏`。

账本只负责"记账 + 风险"，策略负责"什么时候开、什么时候平"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.candles import iso_time
from core.timeframe import SUPPORTED_INTERVALS

EIGHT_HOURS_MS = 8 * 3600 * 1000
VALID_MODES = ("spot", "futures")
VALID_DIRECTIONS = ("long_only", "long_short")


@dataclass(frozen=True)
class TradeConfig:
    mode: str = "spot"
    leverage: float = 1.0
    fee_rate: float = 0.001
    funding_rate_8h: float = 0.0
    maintenance_margin: float = 0.005
    initial_capital: float = 10_000.0
    interval: str = "1m"

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"交易模式必须是 {' / '.join(VALID_MODES)}，收到 {self.mode!r}")
        if not 1.0 <= self.leverage <= 50.0:
            raise ValueError("杠杆倍数应在 [1, 50] 之间")
        if self.mode == "spot" and self.leverage != 1.0:
            raise ValueError("现货模式不能设置杠杆，请切换到合约模式")
        if not 0.0 <= self.fee_rate < 0.05:
            raise ValueError("手续费率应在 [0, 0.05) 之间")
        if not 0.0 <= self.funding_rate_8h < 0.01:
            raise ValueError("资金费率应在 [0, 0.01] / 8h 之间")
        if not 0.0 <= self.maintenance_margin < 0.2:
            raise ValueError("维持保证金率应在 [0, 0.2) 之间")
        if self.initial_capital <= 0:
            raise ValueError("初始资金必须大于 0")
        if self.interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"不支持的 K 线周期：{self.interval!r}")

    @property
    def is_futures(self) -> bool:
        return self.mode == "futures"

    @property
    def effective_leverage(self) -> float:
        return float(self.leverage) if self.is_futures else 1.0


def default_fee_rate(mode: str) -> float:
    """现货 taker 0.1%，U 本位合约 taker 0.05%。"""
    return 0.0005 if mode == "futures" else 0.001


class Ledger:
    """双向持仓账本（现货只允许做多；合约可多可空，同方向可多笔）。"""

    def __init__(self, config: TradeConfig) -> None:
        config.validate()
        self.config = config
        self.cash = float(config.initial_capital)
        self.lots: list[dict] = []
        self.total_fees = 0.0
        self.funding_paid = 0.0  # 正数=净支付，负数=净收取
        self.realized_pnl = 0.0
        self.liquidation_count = 0
        self._last_funding_ts: int | None = None

    # ---------------- 属性 ----------------
    @property
    def leverage(self) -> float:
        return self.config.effective_leverage

    @property
    def locked_margin(self) -> float:
        return sum(lot["margin"] for lot in self.lots)

    def inventory_qty(self, side: str | None = None) -> float:
        return sum(
            lot["qty"] for lot in self.lots if side is None or lot["side"] == side
        )

    def net_qty(self) -> float:
        """净头寸：多头为正、空头为负。"""
        return sum(lot["qty"] if lot["side"] == "long" else -lot["qty"] for lot in self.lots)

    def position_side(self) -> str:
        if not self.lots:
            return "flat"
        sides = {lot["side"] for lot in self.lots}
        return sides.pop() if len(sides) == 1 else "mixed"

    def average_entry(self, side: str | None = None) -> float:
        lots = [lot for lot in self.lots if side is None or lot["side"] == side]
        qty = sum(lot["qty"] for lot in lots)
        if qty <= 0:
            return 0.0
        return sum(lot["qty"] * lot["price"] for lot in lots) / qty

    def equity(self, price: float) -> float:
        unrealized = sum(
            lot["qty"] * (price - lot["price"])
            if lot["side"] == "long"
            else lot["qty"] * (lot["price"] - price)
            for lot in self.lots
        )
        return max(self.cash + self.locked_margin + unrealized, 0.0)

    def max_notional(self) -> float:
        """当前可用现金最多能开出的名义价值（含手续费）。"""
        return max(self.cash, 0.0) / (1.0 / self.leverage + self.config.fee_rate)

    # ---------------- 开仓 / 平仓 ----------------
    def _open(
        self,
        side: str,
        price: float,
        ts: int,
        margin_budget: float,
        reason: str,
        level: int | None,
        meta: dict[str, Any] | None,
    ) -> dict | None:
        if price <= 0 or margin_budget <= 0:
            return None
        if side == "short" and not self.config.is_futures:
            raise ValueError("现货模式不支持做空，请切换到合约模式")
        desired_notional = margin_budget * self.leverage
        notional = min(desired_notional, self.max_notional())
        if notional <= 1e-9:
            return None
        margin = notional / self.leverage
        fee = notional * self.config.fee_rate
        qty = notional / price
        self.cash -= margin + fee
        self.total_fees += fee
        lot = {
            "side": side,
            "price": price,
            "qty": qty,
            "margin": margin,
            "fee": fee,
            "t": ts,
            "level": level,
            "notional": notional,
            "reason": reason,
            "scaled": notional < desired_notional - 1e-9,
        }
        if meta:
            lot.update(meta)
        self.lots.append(lot)
        return lot

    def open_long(
        self,
        price: float,
        ts: int,
        margin_budget: float,
        reason: str = "",
        level: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict | None:
        return self._open("long", price, ts, margin_budget, reason, level, meta)

    def open_short(
        self,
        price: float,
        ts: int,
        margin_budget: float,
        reason: str = "",
        level: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict | None:
        return self._open("short", price, ts, margin_budget, reason, level, meta)

    def open_record(self, lot: dict, ts: int, reason: str | None = None) -> dict:
        return {
            "t": ts,
            "time": iso_time(ts),
            "side": "buy" if lot["side"] == "long" else "sell",
            "position": lot["side"],
            "price": lot["price"],
            "qty": lot["qty"],
            "notional": lot["notional"],
            "fee": lot["fee"],
            "pnl": None,
            "entry_price": None,
            "entry_t": None,
            "entry_time": None,
            "reason": reason if reason is not None else lot.get("reason", ""),
            "level": lot.get("level"),
            "margin": lot["margin"],
        }

    def close_lot(self, index: int, price: float, ts: int, reason: str = "") -> dict | None:
        if not 0 <= index < len(self.lots):
            return None
        lot = self.lots.pop(index)
        if lot["side"] == "long":
            gross = lot["qty"] * (price - lot["price"])
        else:
            gross = lot["qty"] * (lot["price"] - price)
        notional = lot["qty"] * price
        fee = notional * self.config.fee_rate
        self.cash += lot["margin"] + gross - fee
        self.total_fees += fee
        net = gross - fee - lot["fee"]
        self.realized_pnl += net
        return {
            "t": ts,
            "time": iso_time(ts),
            "side": "sell" if lot["side"] == "long" else "buy",
            "position": lot["side"],
            "price": price,
            "qty": lot["qty"],
            "notional": notional,
            "fee": fee,
            "pnl": net,
            "entry_price": lot["price"],
            "entry_t": lot["t"],
            "entry_time": iso_time(lot["t"]),
            "reason": reason or lot.get("reason", ""),
            "level": lot.get("level"),
            "margin": lot["margin"],
        }

    def close_all(self, price: float, ts: int, reason: str = "", aggregate: bool = False) -> list[dict]:
        """全部平仓；`aggregate=True` 把多笔合并成一条（马丁/趋势"一次止盈"口径）。"""
        if not self.lots:
            return []
        side = self.position_side()
        weighted_entry = self.average_entry()
        first_t = min(lot["t"] for lot in self.lots)
        records: list[dict] = []
        while self.lots:
            record = self.close_lot(0, price, ts, reason)
            if record is not None:
                records.append(record)
        if not aggregate:
            return records
        if not records:
            return []
        return [
            {
                "t": ts,
                "time": iso_time(ts),
                "side": "sell" if side == "long" else "buy",
                "position": side,
                "price": price,
                "qty": sum(record["qty"] for record in records),
                "notional": sum(record["notional"] for record in records),
                "fee": sum(record["fee"] for record in records),
                "pnl": sum(record["pnl"] for record in records),
                "entry_price": weighted_entry,
                "entry_t": first_t,
                "entry_time": iso_time(first_t),
                "reason": reason,
                "level": None,
                "margin": sum(record.get("margin", 0.0) for record in records),
            }
        ]

    # ---------------- 强平与资金费率 ----------------
    def liquidation_price(self, lot: dict) -> float:
        if lot["side"] == "long":
            return lot["price"] * (1.0 - 1.0 / self.leverage + self.config.maintenance_margin)
        return lot["price"] * (1.0 + 1.0 / self.leverage - self.config.maintenance_margin)

    def check_liquidation(self, low: float, high: float, ts: int) -> list[dict]:
        """合约强平：多头看最低价、空头看最高价。"""
        if not self.config.is_futures:
            return []
        records: list[dict] = []
        for index in range(len(self.lots) - 1, -1, -1):
            lot = self.lots[index]
            liq_price = self.liquidation_price(lot)
            triggered = (
                low <= liq_price if lot["side"] == "long" else high >= liq_price
            )
            if not triggered:
                continue
            self.lots.pop(index)
            loss = lot["margin"] + lot["fee"]
            self.realized_pnl -= loss
            self.liquidation_count += 1
            records.append(
                {
                    "t": ts,
                    "time": iso_time(ts),
                    "side": "sell" if lot["side"] == "long" else "buy",
                    "position": lot["side"],
                    "price": liq_price,
                    "qty": lot["qty"],
                    "notional": lot["qty"] * liq_price,
                    "fee": 0.0,
                    "pnl": -loss,
                    "entry_price": lot["price"],
                    "entry_t": lot["t"],
                    "entry_time": iso_time(lot["t"]),
                    "reason": f"强平（{self.leverage:.0f}x {'多' if lot['side'] == 'long' else '空'}）",
                    "level": lot.get("level"),
                    "margin": lot["margin"],
                }
            )
        return records

    def apply_funding(self, ts: int, price: float) -> float:
        """合约每 8 小时按**净头寸**名义价值结算：多头支付、空头收取。"""
        if not self.config.is_futures or self.config.funding_rate_8h <= 0:
            return 0.0
        if self._last_funding_ts is None:
            self._last_funding_ts = ts
            return 0.0
        periods = int((ts - self._last_funding_ts) // EIGHT_HOURS_MS)
        if periods <= 0:
            return 0.0
        self._last_funding_ts += periods * EIGHT_HOURS_MS
        net_qty = self.net_qty()
        if abs(net_qty) <= 1e-12:
            return 0.0
        amount = net_qty * price * self.config.funding_rate_8h * periods
        if amount > 0:
            amount = min(amount, max(self.equity(price), 0.0))
        self.cash -= amount
        self.funding_paid += amount
        self.realized_pnl -= amount
        return amount

    # ---------------- 汇总 ----------------
    def extras(self) -> dict:
        return {
            "mode": self.config.mode,
            "leverage": self.leverage,
            "liquidation_count": self.liquidation_count,
            "funding_paid": self.funding_paid,
            "open_position_side": self.position_side(),
        }
