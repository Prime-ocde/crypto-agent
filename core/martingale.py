"""马丁格尔（Martingale）策略回测引擎。

## 规则

1. 空仓 → 以 `initial_order_usdt`（保证金）建仓；
2. 相对**上一次成交价**每下跌 `drop_pct` → 加仓一次，保证金 = `首仓 × multiplier^已有层数`
   （经典 2 倍：100 → 200 → 400 → 800 …，总资金需求是几何级数）；
3. 持仓均价上涨 `take_profit_pct` → **全部止盈**，下一根 K 线重新建仓；
4. 最多 `max_entries` 层（含首仓），资金不足时加仓被跳过并告警；
5. 只做多、不设止损 —— 这正是马丁格尔"高胜率 + 尾部巨亏"的来源；
6. 合约模式（`mode="futures"`）：杠杆放大名义价值，最低价击穿强平价即损失该层保证金，
   每 8 小时按名义价值收资金费。

## 为什么值得放进课程项目

它和网格是**镜像**：网格靠震荡吃价差，马丁格尔靠"不止损 + 摊薄成本"把胜率做到 90%+，
但一次单边下跌就能吞掉前面几十次止盈；加上杠杆后，这个尾部风险会被放大到"一次就出局"。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core import report
from core.candles import iso_time, normalize_candles
from core.execution import Ledger, TradeConfig
from core.timeframe import SUPPORTED_INTERVALS


@dataclass(frozen=True)
class MartingaleParams:
    initial_order_usdt: float = 300.0
    drop_pct: float = 0.005
    multiplier: float = 2.0
    take_profit_pct: float = 0.003
    max_entries: int = 5
    initial_capital: float = 10_000.0
    fee_rate: float = 0.001
    interval: str = "1m"
    mode: str = "spot"
    leverage: float = 1.0
    funding_rate_8h: float = 0.0

    def validate(self) -> None:
        if self.initial_order_usdt <= 0:
            raise ValueError("首次建仓金额必须大于 0")
        if not 0 < self.drop_pct <= 0.5:
            raise ValueError("加仓跌幅应在 (0, 0.5] 之间（0.02 = 每跌 2% 加一次）")
        if not 1.0 <= self.multiplier <= 5.0:
            raise ValueError("加仓倍数应在 [1, 5] 之间（1 = 等额加仓，2 = 经典翻倍）")
        if not 0 < self.take_profit_pct <= 0.5:
            raise ValueError("止盈比例应在 (0, 0.5] 之间（0.01 = 均价 +1% 止盈）")
        if not isinstance(self.max_entries, int) or not 2 <= self.max_entries <= 20:
            raise ValueError("最大加仓层数应为 2~20 的整数（含首次建仓）")
        if self.initial_order_usdt > self.initial_capital:
            raise ValueError("首次建仓金额不能超过初始资金")
        if self.interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"不支持的 K 线周期：{self.interval!r}")
        TradeConfig(
            mode=self.mode,
            leverage=self.leverage,
            fee_rate=self.fee_rate,
            funding_rate_8h=self.funding_rate_8h,
            initial_capital=self.initial_capital,
            interval=self.interval,
        ).validate()

    def required_capital(self) -> float:
        """铺满全部层数需要的保证金（几何级数总和）。"""
        total = 0.0
        size = self.initial_order_usdt
        for _ in range(self.max_entries):
            total += size
            size *= self.multiplier
        return total


def run_martingale_backtest(candles: Any, params: MartingaleParams) -> dict:
    params.validate()
    rows = normalize_candles(candles)

    ledger = Ledger(
        TradeConfig(
            mode=params.mode,
            leverage=params.leverage,
            fee_rate=params.fee_rate,
            funding_rate_8h=params.funding_rate_8h,
            initial_capital=params.initial_capital,
            interval=params.interval,
        )
    )

    cycles = 0
    take_profit_count = 0
    max_layers = 0
    skipped_adds = 0
    pending_entry = True
    last_fill: float | None = None
    first_entry_t: int | None = None

    trades: list[dict] = []
    equity_curve: list[dict] = []
    warnings: list[str] = []

    def _enter(price: float, ts: int, margin: float, reason: str) -> bool:
        nonlocal last_fill, first_entry_t, max_layers
        if price <= 0:
            return False
        lot = ledger.open_long(price, ts, margin, reason=reason)
        if lot is None:
            return False
        max_layers = max(max_layers, len(ledger.lots))
        last_fill = price
        if first_entry_t is None:
            first_entry_t = ts
        trades.append(ledger.open_record(lot, ts, reason))
        return True

    def _close(price: float, ts: int, reason: str) -> None:
        nonlocal cycles, take_profit_count, last_fill, first_entry_t
        if not ledger.lots:
            return
        for record in ledger.close_all(price, ts, reason, aggregate=True):
            trades.append(record)
        cycles += 1
        if reason == "止盈":
            take_profit_count += 1
        last_fill = None
        first_entry_t = None

    def _sync_after_liquidation() -> None:
        """强平后同步马丁格尔的加仓状态，避免继续按已消失的层数加仓。"""
        nonlocal last_fill, first_entry_t
        if not ledger.lots:
            last_fill = None
            first_entry_t = None
            return
        last_fill = ledger.lots[-1]["price"]
        first_entry_t = ledger.lots[0]["t"]

    for candle in rows:
        open_, high, low, close = candle["o"], candle["h"], candle["l"], candle["c"]
        ts = candle["t"]

        if not ledger.lots and pending_entry:
            if _enter(open_, ts, params.initial_order_usdt, "建仓"):
                pending_entry = False

        path = [open_, low, high, close] if close >= open_ else [open_, high, low, close]
        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:  # 下跌：检查加仓
                while ledger.lots and len(ledger.lots) < params.max_entries and last_fill is not None:
                    trigger = last_fill * (1.0 - params.drop_pct)
                    if p1 > trigger:
                        break
                    size = params.initial_order_usdt * (params.multiplier ** (len(ledger.lots)))
                    if ledger.max_notional() + 1e-9 < size * ledger.leverage:
                        skipped_adds += 1
                        break
                    if not _enter(trigger, ts, size, f"第 {len(ledger.lots) + 1} 层加仓"):
                        skipped_adds += 1
                        break
            elif p1 > p0 and ledger.lots:  # 上涨：检查止盈
                take_profit_price = ledger.average_entry() * (1.0 + params.take_profit_pct)
                if p1 >= take_profit_price:
                    _close(take_profit_price, ts, "止盈")
                    pending_entry = True

        for record in ledger.check_liquidation(low, high, ts):
            trades.append(record)
        if ledger.liquidation_count:
            _sync_after_liquidation()
            if not ledger.lots:
                pending_entry = True

        ledger.apply_funding(ts, close)
        equity_curve.append(
            {"t": ts, "time": iso_time(ts), "price": close, "equity": ledger.equity(close)}
        )

    last_close = rows[-1]["c"]
    open_qty = ledger.inventory_qty()
    average_entry = ledger.average_entry()

    summary = report.build_summary(
        initial_capital=params.initial_capital,
        equity_curve=equity_curve,
        trades=trades,
        interval=params.interval,
        open_position_qty=open_qty,
        open_position_value=open_qty * last_close,
        total_fees=ledger.total_fees,
        realized_pnl=ledger.realized_pnl,
        extras={
            "layers_max": max_layers,
            "cycles": cycles,
            "take_profit_count": take_profit_count,
            "skipped_adds": skipped_adds,
            "required_capital": params.required_capital(),
            **ledger.extras(),
        },
    )

    required = params.required_capital()
    if required > params.initial_capital:
        warnings.append(
            f"按当前参数铺满 {params.max_entries} 层需要 {required:,.2f} USDT 保证金（几何级数总和），"
            f"超过可用资金 {params.initial_capital:,.2f}：加仓链会在中途因资金不足断裂。"
        )
    if skipped_adds > 0:
        warnings.append(
            f"有 {skipped_adds} 次加仓因可用资金不足被跳过。马丁格尔的仓位是几何级数增长，"
            "资金曲线恰恰会在最需要摊薄成本的时候断掉。"
        )
    if max_layers >= params.max_entries:
        warnings.append(
            f"本次回测触达最大加仓层数 {params.max_entries} 层：真实交易中意味着已经满仓，"
            "价格继续下跌就只能扛单。"
        )
    if open_qty > 0:
        warnings.append(
            f"期末仍有持仓（{open_qty:.6f}，均价 {average_entry:,.2f}），已按最新价市值重估。"
            "马丁格尔不设止损，扛单是常态，也是它最大的风险来源。"
        )
    if ledger.liquidation_count > 0:
        warnings.append(
            f"合约模式下有 {ledger.liquidation_count} 笔仓位被强平：杠杆会先于「反弹止盈」到来，"
            "马丁格尔在合约上最典型的死法就是「加仓加到爆仓」。"
        )
    if ledger.funding_paid > 1e-6:
        warnings.append(
            f"资金费率累计支出 {ledger.funding_paid:,.2f} USDT（每 8 小时按名义价值收取），已计入净值。"
        )
    warnings.append(
        "马丁格尔的高胜率来自「不止损 + 加仓摊平」：一次单边下跌可以吞掉前面几十次止盈，"
        "请重点看最大回撤与单笔最大亏损，不要只看胜率。"
    )

    return {
        "strategy": "martingale",
        "strategy_label": "马丁格尔",
        "params": asdict(params),
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "overlays": {"grid_levels": [], "lines": []},
        "highlight": {
            "label": "最大加仓层数",
            "value": f"{max_layers}/{params.max_entries}",
            "help": "本次回测中实际用到的最大层数 / 允许的最大层数",
        },
        "warnings": warnings,
        "meta": {
            "engine": "martingale-v2",
            "assumptions": [
                "加仓按「上一次成交价 × (1 − drop_pct)」触发，并按触发价撮合",
                "止盈按持仓均价（不含手续费）× (1 + take_profit_pct) 全部平仓",
                "只做多、不设止损；合约模式下使用杠杆并按最低价检查强平",
                "单根 K 线按 open→低→高→close 路径模拟，因此一根 K 线内可能同时加仓与止盈",
            ],
            "required_capital": params.required_capital(),
            "mode": params.mode,
            "leverage": ledger.leverage,
        },
    }
