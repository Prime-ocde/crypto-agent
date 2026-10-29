"""手动交易账户：把 `core.execution.Ledger` 包成「可下单 / 可推进 / 可落盘」的账户对象。

## 为什么不是新策略

手动交易不需要策略注册表：它没有「信号」，只有「用户点击」。但它是**有状态、要跨
rerun 存活、还要落盘**的，所以这里做一个薄封装，把三件事和非 UI 逻辑分开：

1. **下单/平仓**：参数校验、保证金预算、记录成交；
2. **tick**：资金费结算 + 强平检查（顺序不能反：先收资金费再判断爆仓，与回测一致）；
3. **落盘**：`Ledger.to_dict()` + 成交记录 → JSON，原子写入；恢复时重建账本。

## 与回测的口径差异（有意为之）

- 回测里资金费/强平按「每根 K 线的最低/最高价」近似；手动账户只有一个**当前价**，
  所以 `tick()` 把当前价同时当作 low/high —— 更保守也更诚实，不假装知道未来；
- 杠杆**按笔锁定**（见 `core/execution.py`）：持仓期间改杠杆只影响新仓位，
  已开仓位的强平价不变；
- 账户文件是**内存模拟盘的持久化**，不是券商对账单：没有滑点、没有部分成交。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from core.execution import Ledger, TradeConfig, default_fee_rate

SCHEMA_VERSION = 1
MAX_RECORDS = 500
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "manual"


def now_ms() -> int:
    return int(time.time() * 1000)


def default_path(symbol: str, data_dir: Path | None = None) -> Path:
    """每个交易对一个账户文件：切换交易对不会把两个账户搅在一起。"""
    safe = "".join(ch for ch in str(symbol).upper() if ch.isalnum()) or "ACCOUNT"
    return (data_dir or DEFAULT_DATA_DIR) / f"{safe}.json"


class ManualAccount:
    """手动交易账户（合约优先；现货只做多）。"""

    def __init__(
        self,
        symbol: str,
        ledger: Ledger,
        records: Iterable[dict] | None = None,
        liquidations: Iterable[dict] | None = None,
        created_at: int | None = None,
        path: Path | str | None = None,
    ) -> None:
        self.symbol = str(symbol).upper()
        self.ledger = ledger
        self.records: list[dict] = list(records or [])
        self.liquidations: list[dict] = list(liquidations or [])
        self.created_at = int(created_at or now_ms())
        self.updated_at = self.created_at
        self.path = Path(path) if path else None

    # ---------------- 构造 ----------------
    @classmethod
    def create(
        cls,
        symbol: str,
        capital: float = 10_000.0,
        leverage: float = 10.0,
        mode: str = "futures",
        fee_rate: float | None = None,
        funding_rate_8h: float = 0.0001,
        maintenance_margin: float = 0.005,
        interval: str = "1m",
        path: Path | str | None = None,
    ) -> "ManualAccount":
        config = TradeConfig(
            mode=mode,
            leverage=1.0 if mode == "spot" else leverage,
            fee_rate=default_fee_rate(mode) if fee_rate is None else fee_rate,
            funding_rate_8h=funding_rate_8h if mode == "futures" else 0.0,
            maintenance_margin=maintenance_margin,
            initial_capital=capital,
            interval=interval,
        )
        ledger = Ledger(config)  # 内部会 validate，非法参数直接抛
        return cls(symbol, ledger, path=path or default_path(symbol))

    @classmethod
    def load(cls, path: Path | str, symbol: str | None = None) -> "ManualAccount":
        """读取落盘账户；文件缺失/损坏/版本不符/交易对不符都抛 ValueError。"""
        file_path = Path(path)
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"账户文件不存在：{file_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"账户文件无法解析：{file_path}（{type(exc).__name__}）") from exc

        version = payload.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"账户文件版本不兼容：{version!r}（期望 {SCHEMA_VERSION}）")
        saved_symbol = str(payload.get("symbol") or "").upper()
        if symbol and saved_symbol and saved_symbol != str(symbol).upper():
            raise ValueError(f"账户属于 {saved_symbol}，与当前 {symbol} 不匹配")

        ledger_data = payload.get("ledger")
        if not isinstance(ledger_data, dict):
            raise ValueError("账户文件缺少 ledger")
        account = cls(
            saved_symbol or symbol or "ACCOUNT",
            Ledger.from_dict(ledger_data),
            records=payload.get("records") or [],
            liquidations=payload.get("liquidations") or [],
            created_at=payload.get("created_at"),
            path=file_path,
        )
        account.updated_at = int(payload.get("updated_at") or account.created_at)
        return account

    @classmethod
    def load_or_create(cls, symbol: str, **kwargs: Any) -> "ManualAccount":
        """有存档就恢复，没有（或损坏）就按给定参数新建 —— 页面刷新不丢仓位。"""
        path = kwargs.get("path") or default_path(symbol)
        try:
            return cls.load(path, symbol=symbol)
        except ValueError:
            kwargs.pop("path", None)
            return cls.create(symbol, path=path, **kwargs)

    # ---------------- 落盘 ----------------
    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ledger": self.ledger.to_dict(),
            "records": self.records[-MAX_RECORDS:],
            "liquidations": self.liquidations[-MAX_RECORDS:],
        }

    def save(self, path: Path | str | None = None) -> Path:
        """原子写入（先写临时文件再 replace），避免刷新时读到半个 JSON。"""
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("未指定账户文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = now_ms()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        self.path = target
        return target

    # ---------------- 下单 / 平仓 ----------------
    @property
    def leverage(self) -> float:
        return self.ledger.leverage

    def set_leverage(self, leverage: float) -> None:
        """改杠杆只影响**新仓位**；有持仓时直接拒绝，避免用户误以为旧仓位也变了。"""
        value = float(leverage)
        if self.ledger.lots:
            raise ValueError("有持仓时不能改杠杆，请先平仓（已开仓位的强平价按建仓杠杆锁定）")
        config = TradeConfig(
            mode=self.ledger.config.mode,
            leverage=value if self.ledger.config.mode == "futures" else 1.0,
            fee_rate=self.ledger.config.fee_rate,
            funding_rate_8h=self.ledger.config.funding_rate_8h,
            maintenance_margin=self.ledger.config.maintenance_margin,
            initial_capital=self.ledger.config.initial_capital,
            interval=self.ledger.config.interval,
        )
        config.validate()
        self.ledger.config = config

    def open(
        self,
        side: str,
        price: float,
        ts: int,
        margin_usdt: float | None = None,
        margin_pct: float = 1.0,
        reason: str = "手动开仓",
    ) -> dict | None:
        """按保证金下单；`margin_usdt` 优先级高于 `margin_pct`。"""
        direction = str(side).lower()
        if direction not in ("long", "short"):
            raise ValueError(f"方向必须是 long / short，收到 {side!r}")
        if price <= 0:
            raise ValueError("价格必须大于 0")
        if self.ledger.equity(price) <= 0:
            raise ValueError("账户权益已归零，请重置资金后再交易")

        budget = float(margin_usdt) if margin_usdt is not None else self.ledger.cash * float(margin_pct)
        budget = min(max(budget, 0.0), max(self.ledger.cash, 0.0))
        if budget <= 0:
            raise ValueError("可用现金不足，无法开仓")

        opener = self.ledger.open_long if direction == "long" else self.ledger.open_short
        lot = opener(price, ts, budget, reason)
        if lot is None:
            return None
        self.records.append(self.ledger.open_record(lot, ts, reason))
        self._trim()
        return lot

    def close(self, price: float, ts: int, lot_index: int | None = None, reason: str = "手动平仓") -> list[dict]:
        """平掉指定一笔或全部；返回成交记录列表（不含手续费口径以外的加工）。"""
        if not self.ledger.lots:
            return []
        if lot_index is None:
            records = self.ledger.close_all(price, ts, reason, aggregate=False)
        else:
            record = self.ledger.close_lot(int(lot_index), price, ts, reason)
            records = [record] if record else []
        self.records.extend(records)
        self._trim()
        return records

    # ---------------- 实时推进 ----------------
    def tick(self, price: float, ts: int | None = None) -> dict:
        """每个报价周期调用：先结算资金费，再检查强平（与回测顺序一致）。"""
        ts = int(ts if ts is not None else now_ms())
        before = len(self.ledger.lots)
        funding = self.ledger.apply_funding(ts, price)
        liquidations = self.ledger.check_liquidation(price, price, ts)
        if liquidations:
            self.records.extend(liquidations)
            self.liquidations.extend(liquidations)
            self._trim()
        return {
            "ts": ts,
            "funding": funding,
            "liquidations": liquidations,
            "equity": self.ledger.equity(price),
            "position_closed": bool(liquidations) or (before > 0 and not self.ledger.lots),
        }

    def _trim(self) -> None:
        if len(self.records) > MAX_RECORDS:
            self.records = self.records[-MAX_RECORDS:]

    # ---------------- 快照（给 UI 读） ----------------
    def _nearest_liquidation(self) -> float | None:
        if not self.ledger.lots:
            return None
        prices = [self.ledger.liquidation_price(lot) for lot in self.ledger.lots]
        # 多头跌到强平价爆、空头涨到强平价爆：先被碰到的那条才是"最近"的
        return max(prices) if self.ledger.position_side() == "long" else min(prices)

    def snapshot(self, price: float) -> dict:
        ledger = self.ledger
        side = ledger.position_side()
        equity = ledger.equity(price)
        locked = ledger.locked_margin
        qty = ledger.inventory_qty(side) if side in ("long", "short") else 0.0
        avg_entry = ledger.average_entry(side) if side in ("long", "short") else 0.0
        unrealized = 0.0
        for lot in ledger.lots:
            if lot["side"] == "long":
                unrealized += lot["qty"] * (price - lot["price"])
            else:
                unrealized += lot["qty"] * (lot["price"] - price)

        liq_price = self._nearest_liquidation()
        liq_distance_pct = None
        if liq_price is not None and price > 0:
            if side == "long":
                liq_distance_pct = (price - liq_price) / price
            elif side == "short":
                liq_distance_pct = (liq_price - price) / price

        risk_ratio = locked / equity if equity > 0 else 1.0
        notional = qty * price
        funding_estimate = (
            notional * ledger.config.funding_rate_8h
            if ledger.config.is_futures and side in ("long", "short")
            else 0.0
        )

        snapshot = {
            "symbol": self.symbol,
            "mode": ledger.config.mode,
            "leverage": ledger.leverage,
            "position": side,
            "qty": qty,
            "avg_entry": avg_entry,
            "notional": notional,
            "equity": equity,
            "cash": ledger.cash,
            "locked_margin": locked,
            "unrealized_pnl": unrealized,
            "unrealized_pct_of_margin": (unrealized / locked) if locked > 0 else 0.0,
            "realized_pnl": ledger.realized_pnl,
            "total_fees": ledger.total_fees,
            "funding_paid": ledger.funding_paid,
            "liquidation_count": ledger.liquidation_count,
            "risk_ratio": risk_ratio,
            "liq_price": liq_price,
            "liq_distance_pct": liq_distance_pct,
            "funding_estimate_8h": funding_estimate,
            "open_lots": len(ledger.lots),
            "can_open": equity > 0 and ledger.cash > 0,
        }
        snapshot["warnings"] = self._warnings(snapshot)
        return snapshot

    @staticmethod
    def _warnings(snapshot: dict) -> list[dict]:
        """三档强平预警：0.5% 内=爆仓边缘、2% 内=危险、5% 内=注意。"""
        warnings: list[dict] = []
        if snapshot["position"] == "flat":
            return warnings
        distance = snapshot.get("liq_distance_pct")
        if distance is not None:
            pct = distance * 100
            if pct <= 0.5:
                warnings.append({"level": "error", "text": f"距强平仅 {pct:.2f}%，任何一次插针都可能爆仓"})
            elif pct <= 2.0:
                warnings.append({"level": "error", "text": f"距强平 {pct:.2f}%，高杠杆下反转极快"})
            elif pct <= 5.0:
                warnings.append({"level": "warning", "text": f"距强平 {pct:.2f}%，注意减仓或补保证金"})
        if snapshot["risk_ratio"] >= 0.9:
            warnings.append(
                {"level": "warning", "text": f"保证金占用率 {snapshot['risk_ratio'] * 100:.0f}%，余量很小"}
            )
        funding = snapshot.get("funding_estimate_8h") or 0.0
        if abs(funding) >= 1e-9:
            verb = "支付" if funding > 0 else "收取"
            warnings.append(
                {"level": "info", "text": f"每 8 小时{verb}资金费约 {abs(funding):,.2f} USDT（按当前名义价值）"}
            )
        return warnings
