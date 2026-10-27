"""趋势跟踪（Trend Following）策略回测引擎：双均线 + 止损/止盈 + 可选做空。

## 规则

1. 用收盘价计算快线 / 慢线（简单移动平均）；
2. **快线在慢线上方（多头排列）→ 下一根 K 线开盘做多**；跌到下方 → 平多；
3. 合约模式且 `direction="long_short"` 时：**空头排列 → 下一根开盘做空**，涨回上方 → 平空；
4. 持仓期间：价格触及 `stop_loss_pct` → 止损；触及 `take_profit_pct`（>0）→ 止盈；
5. 合约模式下按杠杆放大名义价值、检查强平、每 8 小时结算资金费（空头收取）。

> 用"状态判断"而不是"严格金叉那一根"：慢线刚有值的那一根如果已经是多头排列，
> 也应该允许建仓，否则会漏掉样本开头的整段趋势。

## 为什么"下一根开盘执行"

如果拿第 i 根收盘价算出的信号，却按第 i 根收盘价成交，就是**未来函数**（lookahead bias），
回测会凭空变好看。这里严格用 `i` 根收盘确认信号、`i+1` 根开盘成交。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from core import report
from core.candles import iso_time, normalize_candles
from core.execution import VALID_DIRECTIONS, Ledger, TradeConfig
from core.timeframe import SUPPORTED_INTERVALS, interval_ms


@dataclass(frozen=True)
class TrendParams:
    fast_ma: int = 10
    slow_ma: int = 30
    position_pct: float = 1.0
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.0
    initial_capital: float = 10_000.0
    fee_rate: float = 0.001
    interval: str = "1m"
    mode: str = "spot"
    leverage: float = 1.0
    funding_rate_8h: float = 0.0
    direction: str = "long_only"

    def validate(self) -> None:
        if not isinstance(self.fast_ma, int) or not isinstance(self.slow_ma, int):
            raise ValueError("均线周期必须是整数")
        if self.fast_ma < 2:
            raise ValueError("快线周期至少为 2")
        if self.slow_ma <= self.fast_ma:
            raise ValueError("慢线周期必须大于快线周期")
        if self.slow_ma > 500:
            raise ValueError("慢线周期不要超过 500（样本根本不够）")
        if not 0 < self.position_pct <= 1.0:
            raise ValueError("仓位比例应在 (0, 1] 之间")
        if not 0.0 <= self.stop_loss_pct < 0.5:
            raise ValueError("止损比例应在 [0, 0.5) 之间（0 = 不设止损）")
        if not 0.0 <= self.take_profit_pct < 0.5:
            raise ValueError("止盈比例应在 [0, 0.5) 之间（0 = 不设止盈）")
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(f"方向必须是 {' / '.join(VALID_DIRECTIONS)}，收到 {self.direction!r}")
        if self.mode == "spot" and self.direction == "long_short":
            raise ValueError("现货模式不支持做空，请切换到合约模式")
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


def _sma(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index >= window - 1:
            out[index] = total / window
    return out


def run_trend_backtest(candles: Any, params: TrendParams) -> dict:
    params.validate()
    rows = normalize_candles(candles)
    closes = [row["c"] for row in rows]
    fast_series = _sma(closes, params.fast_ma)
    slow_series = _sma(closes, params.slow_ma)
    bar_ms = interval_ms(params.interval)

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

    position_side: str | None = None
    entry_price = 0.0
    entry_t: int | None = None
    stop_count = 0
    take_profit_count = 0
    hold_bars: list[float] = []
    signal: str | None = None

    trades: list[dict] = []
    equity_curve: list[dict] = []
    warnings: list[str] = []

    def _open(side: str, price: float, ts: int, reason: str) -> None:
        nonlocal position_side, entry_price, entry_t
        if ledger.lots or price <= 0:
            return
        margin = ledger.cash * params.position_pct
        lot = (
            ledger.open_short(price, ts, margin, reason=reason)
            if side == "short"
            else ledger.open_long(price, ts, margin, reason=reason)
        )
        if lot is None:
            return
        position_side = side
        entry_price = lot["price"]
        entry_t = ts
        trades.append(ledger.open_record(lot, ts, reason))

    def _close(price: float, ts: int, reason: str) -> None:
        nonlocal position_side, entry_price, entry_t
        if not ledger.lots:
            return
        if entry_t is not None:
            hold_bars.append((ts - entry_t) / bar_ms)
        for record in ledger.close_all(price, ts, reason, aggregate=True):
            trades.append(record)
        position_side = None
        entry_price = 0.0
        entry_t = None

    for index, candle in enumerate(rows):
        open_, high, low, close = candle["o"], candle["h"], candle["l"], candle["c"]
        ts = candle["t"]

        # 1) 开盘执行上一根收盘确认的信号（含反手）
        if signal is not None:
            target: str | None = signal
            if target == "short" and params.direction != "long_short":
                target = None  # 只做多：空头信号只用于平多
            if position_side is not None and position_side != target:
                _close(open_, ts, "均线转空平仓" if position_side == "long" else "均线转多平仓")
            if position_side is None and target is not None:
                _open(
                    target,
                    open_,
                    ts,
                    "均线多头开仓" if target == "long" else "均线空头开仓",
                )
        signal = None

        # 2) 盘中检查止损 / 止盈（多头看下跌、空头看上涨）
        if ledger.lots and position_side is not None:
            if position_side == "long":
                stop_price = entry_price * (1.0 - params.stop_loss_pct) if params.stop_loss_pct > 0 else None
                take_price = entry_price * (1.0 + params.take_profit_pct) if params.take_profit_pct > 0 else None
            else:
                stop_price = entry_price * (1.0 + params.stop_loss_pct) if params.stop_loss_pct > 0 else None
                take_price = entry_price * (1.0 - params.take_profit_pct) if params.take_profit_pct > 0 else None

            path = [open_, low, high, close] if close >= open_ else [open_, high, low, close]
            for p0, p1 in zip(path, path[1:]):
                if not ledger.lots:
                    break
                if p1 < p0:  # 下跌
                    if position_side == "long" and stop_price is not None and p1 <= stop_price:
                        _close(min(p0, stop_price), ts, "止损")
                        stop_count += 1
                        break
                    if position_side == "short" and take_price is not None and p1 <= take_price:
                        _close(min(p0, take_price), ts, "止盈")
                        take_profit_count += 1
                        break
                elif p1 > p0:  # 上涨
                    if position_side == "long" and take_price is not None and p1 >= take_price:
                        _close(max(p0, take_price), ts, "止盈")
                        take_profit_count += 1
                        break
                    if position_side == "short" and stop_price is not None and p1 >= stop_price:
                        _close(max(p0, stop_price), ts, "止损")
                        stop_count += 1
                        break

        # 3) 合约强平（多头看最低价、空头看最高价）
        for record in ledger.check_liquidation(low, high, ts):
            trades.append(record)
        if ledger.liquidation_count and not ledger.lots:
            position_side = None
            entry_price = 0.0
            entry_t = None

        ledger.apply_funding(ts, close)

        # 4) 收盘后用已收盘数据判断多空排列，供下一根开盘执行（杜绝未来函数）
        if fast_series[index] is not None and slow_series[index] is not None:
            signal = "long" if fast_series[index] > slow_series[index] else "short"

        equity_curve.append(
            {"t": ts, "time": iso_time(ts), "price": close, "equity": ledger.equity(close)}
        )

    last_close = rows[-1]["c"]
    net_qty = ledger.net_qty()
    summary = report.build_summary(
        initial_capital=params.initial_capital,
        equity_curve=equity_curve,
        trades=trades,
        interval=params.interval,
        open_position_qty=net_qty,
        open_position_value=net_qty * last_close,
        total_fees=ledger.total_fees,
        realized_pnl=ledger.realized_pnl,
        extras={
            "stop_loss_count": stop_count,
            "take_profit_count": take_profit_count,
            "avg_hold_bars": (sum(hold_bars) / len(hold_bars)) if hold_bars else 0.0,
            **ledger.extras(),
        },
    )

    warmup = params.slow_ma + 2
    if len(rows) < warmup:
        warnings.append(
            f"样本不足：慢线需要 {params.slow_ma} 根、加上交叉确认共 {warmup} 根 K 线，"
            f"当前只有 {len(rows)} 根，趋势策略无法产生有效信号。"
        )
    elif summary["trade_count"] == 0:
        warnings.append(
            f"这段行情里 {params.fast_ma}/{params.slow_ma} 均线没有出现可交易的方向信号，趋势策略全程空仓等待。"
        )
    if net_qty != 0:
        side_text = "多头" if net_qty > 0 else "空头"
        warnings.append(
            f"期末仍持有{side_text}（{abs(net_qty):.6f}，开仓价 {entry_price:,.2f}），已按最新价市值重估。"
        )
    if stop_count > 0:
        warnings.append(
            f"期间触发 {stop_count} 次止损：震荡行情里均线策略会被反复「上下插针」打止损，"
            "这是趋势策略最典型的成本（whipsaw）。"
        )
    if ledger.liquidation_count > 0:
        warnings.append(
            f"合约模式下有 {ledger.liquidation_count} 次强平：止损还没成交，强平线先到了。"
            "杠杆越高，止损空间越容易被吃掉。"
        )
    if abs(ledger.funding_paid) > 1e-6:
        if ledger.funding_paid > 0:
            warnings.append(
                f"资金费率累计支出 {ledger.funding_paid:,.2f} USDT（每 8 小时按净头寸名义价值结算），已计入净值。"
            )
        else:
            warnings.append(
                f"资金费率累计收取 {-ledger.funding_paid:,.2f} USDT（空头仓位收到资金费），已计入净值。"
            )

    line_points_fast = [
        [rows[index]["t"], value] for index, value in enumerate(fast_series) if value is not None
    ]
    line_points_slow = [
        [rows[index]["t"], value] for index, value in enumerate(slow_series) if value is not None
    ]

    return {
        "strategy": "trend",
        "strategy_label": "趋势跟踪",
        "params": asdict(params),
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "overlays": {
            "grid_levels": [],
            "lines": [
                {"name": f"SMA{params.fast_ma}", "color": "#4fc3f7", "points": line_points_fast},
                {"name": f"SMA{params.slow_ma}", "color": "#ffb74d", "points": line_points_slow},
            ],
        },
        "highlight": {
            "label": "止损次数",
            "value": f"{stop_count}",
            "help": "多空两侧止损被打掉的次数；震荡行情里这个数字会很高",
        },
        "warnings": warnings,
        "meta": {
            "engine": "trend-v3",
            "assumptions": [
                "信号用第 i 根收盘价计算、第 i+1 根开盘价成交（严格无未来函数）",
                "止损/止盈按价格触碰近似成交（跳空时按开盘价成交）",
                f"交易方向：{'多空双向' if params.direction == 'long_short' else '仅做多'}",
                "合约模式下按杠杆放大名义价值；多头最低价、空头最高价击穿强平价即损失保证金",
                "均线为简单移动平均（SMA），未做复权处理",
            ],
            "mode": params.mode,
            "leverage": ledger.leverage,
            "direction": params.direction,
        },
    }
