"""网格交易回测引擎（纯函数、只依赖标准库）。

## 策略假设（老师一定会问，先把假设写清楚）

1. 网格线在 `[lower, upper]` 之间等距切分，共 `grid_count + 1` 条线；
2. 价格**向下**穿越某条网格线 → 按该线价格买入一份；**向上**穿越 → 卖出最近一档已持仓的仓位
   （限价单近似成交，不含滑点）；
3. **每条网格线最多持有一份仓位**：该格卖出后才重新挂单，避免同价位反复加仓；
4. 每份买入预算 = `initial_capital / grid_count`（这是**保证金**预算，合约模式下会放大成名义价值）；
5. 买卖各按成交额扣手续费；只做多；
6. 路径模拟：每根 K 线按 `open → 低 → 高 → close`（阳线）或
   `open → 高 → 低 → close`（阴线）走，单根 K 线内多次穿越也能成交；
7. 合约模式（`mode="futures"`）：按 `leverage` 放大名义价值、检查强平、每 8 小时收资金费。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core import report
from core.candles import iso_time, normalize_candles
from core.execution import Ledger, TradeConfig
from core.timeframe import bars_per_year, SUPPORTED_INTERVALS

_EPS = 1e-12
_MAX_GRID_COUNT = 500


@dataclass(frozen=True)
class GridParams:
    """回测参数（不可变，避免调用方中途改参数导致结果不可复现）。"""

    lower: float
    upper: float
    grid_count: int
    initial_capital: float = 10_000.0
    fee_rate: float = 0.001
    interval: str = "1m"
    mode: str = "spot"
    leverage: float = 1.0
    funding_rate_8h: float = 0.0

    def validate(self) -> None:
        if not isinstance(self.grid_count, int):
            raise ValueError("网格数量必须是整数")
        if self.lower <= 0:
            raise ValueError("网格下界必须大于 0")
        if self.upper <= self.lower:
            raise ValueError("网格上界必须大于网格下界")
        if self.grid_count < 2:
            raise ValueError("网格数量至少为 2（1 个网格切不出上下两条线）")
        if self.grid_count > _MAX_GRID_COUNT:
            raise ValueError(f"网格数量不要超过 {_MAX_GRID_COUNT}（演示引擎，再多也没有信息增量）")
        if self.interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"不支持的 K 线周期：{self.interval!r}，可选：{', '.join(SUPPORTED_INTERVALS)}"
            )
        if (self.upper - self.lower) / self.grid_count <= self.upper * 1e-9:
            raise ValueError("网格间距过小，请减少网格数量或扩大区间")
        TradeConfig(
            mode=self.mode,
            leverage=self.leverage,
            fee_rate=self.fee_rate,
            funding_rate_8h=self.funding_rate_8h,
            initial_capital=self.initial_capital,
            interval=self.interval,
        ).validate()


def _grid_levels(params: GridParams) -> list[float]:
    step = (params.upper - params.lower) / params.grid_count
    return [round(params.lower + i * step, 10) for i in range(params.grid_count + 1)]


def run_grid_backtest(
    candles: Any,
    params: GridParams,
    *,
    liquidate_at_end: bool = False,
) -> dict:
    """执行一次网格回测。"""
    params.validate()
    rows = normalize_candles(candles)
    levels = _grid_levels(params)
    step = (params.upper - params.lower) / params.grid_count
    margin_budget = params.initial_capital / params.grid_count
    fee_rate = params.fee_rate

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

    # 每条网格线最多持有一份仓位：key = 网格线序号
    lots: dict[int, dict] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    warnings: list[str] = []
    skipped_buys = 0
    touched_levels: set[int] = set()

    def _buy(level_idx: int, price: float, ts: int) -> None:
        nonlocal skipped_buys
        if level_idx in lots:
            return  # 该网格已持仓，不重复买入
        if ledger.max_notional() + 1e-9 < margin_budget * ledger.leverage:
            skipped_buys += 1
            return
        lot = ledger.open_long(
            price,
            ts,
            margin_budget,
            reason=f"网格买入 L{level_idx}",
            level=level_idx,
        )
        if lot is None:
            skipped_buys += 1
            return
        lots[level_idx] = lot
        touched_levels.add(level_idx)
        trades.append(ledger.open_record(lot, ts))

    def _sell(level_idx: int, price: float, ts: int) -> None:
        candidates = [idx for idx in lots if idx < level_idx]
        if not candidates:
            return
        buy_idx = max(candidates)
        lot = lots.pop(buy_idx)
        try:
            index = ledger.lots.index(lot)
        except ValueError:
            return
        record = ledger.close_lot(index, price, ts, reason="网格卖出")
        if record is not None:
            trades.append(record)

    for candle in rows:
        open_, high, low, close = candle["o"], candle["h"], candle["l"], candle["c"]
        ts = candle["t"]

        # 阳线：开 → 低 → 高 → 收；阴线：开 → 高 → 低 → 收
        path = [open_, low, high, close] if close >= open_ else [open_, high, low, close]

        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:  # 下跌：穿越的网格线按价格从高到低依次买入
                for idx in range(params.grid_count, -1, -1):
                    level = levels[idx]
                    if p1 - _EPS <= level < p0 - _EPS:
                        _buy(idx, level, ts)
            elif p1 > p0:  # 上涨：穿越的网格线按价格从低到高依次卖出
                for idx in range(0, params.grid_count + 1):
                    level = levels[idx]
                    if p0 + _EPS < level <= p1 + _EPS:
                        _sell(idx, level, ts)

        # 合约：最低价击穿强平价 → 该格保证金全部损失
        for record in ledger.check_liquidation(low, high, ts):
            trades.append(record)
        if ledger.liquidation_count:
            for level_idx in [idx for idx, lot in lots.items() if lot not in ledger.lots]:
                lots.pop(level_idx, None)

        # 合约：每 8 小时按名义价值收资金费
        ledger.apply_funding(ts, close)

        equity_curve.append(
            {
                "t": ts,
                "time": iso_time(ts),
                "price": close,
                "equity": ledger.equity(close),
            }
        )

    last = rows[-1]
    last_close = last["c"]

    if liquidate_at_end and ledger.lots:
        for record in ledger.close_all(last_close, last["t"], "期末清仓"):
            trades.append(record)
        lots.clear()
        equity_curve[-1]["equity"] = ledger.equity(last_close)

    open_qty = ledger.inventory_qty()
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
            "touch_level_count": len(touched_levels),
            "skipped_buy_signals": skipped_buys,
            **ledger.extras(),
        },
    )

    if summary["trade_count"] == 0:
        if last_close > params.upper:
            warnings.append(
                f"整段行情都在网格上界之上（最新价 {last_close:,.2f} > 上界 {params.upper:,.2f}），"
                "网格从未触发。请把上界抬高，或改造成追涨型网格。"
            )
        elif last_close < params.lower:
            warnings.append(
                f"整段行情都在网格下界之下（最新价 {last_close:,.2f} < 下界 {params.lower:,.2f}），"
                "网格从未触发。请把下界压低后再回测。"
            )
        else:
            warnings.append("行情在网格区间内但振幅小于一个网格间距，未触发任何成交。可增加网格数量或缩小价格区间。")

    if skipped_buys > 0:
        warnings.append(
            f"有 {skipped_buys} 次买入信号因可用资金不足被跳过：每格保证金预算 = 初始资金 / 网格数量，"
            "铺满全部网格后已无剩余资金，属于正常现象。"
        )

    mid_price = (params.lower + params.upper) / 2.0
    step_pct = step / mid_price if mid_price > 0 else 0.0
    if step_pct <= fee_rate * 2:
        warnings.append(
            f"网格间距只有 {step_pct * 100:.3f}%，小于双边手续费合计 {fee_rate * 200:.3f}%："
            "在理想成交下，每一个「买入 → 卖出」循环都是净亏损，赚的钱不够付手续费。"
            "请减少网格数量或放宽网格区间（经验值：间距 ≥ 2~3 倍双边手续费）。"
        )

    if last_close > params.upper or last_close < params.lower:
        warnings.append("最新价已跑出网格区间，继续运行时网格将暂停成交（只会在价格回到区间内后恢复）。")

    if ledger.liquidation_count > 0:
        warnings.append(
            f"合约模式下有 {ledger.liquidation_count} 笔仓位被强平：网格「越跌越买」在杠杆下会直接撞上强平线，"
            "这是把网格搬到合约上最危险的地方。"
        )
    if ledger.funding_paid > 1e-6:
        warnings.append(
            f"资金费率累计支出 {ledger.funding_paid:,.2f} USDT（每 8 小时按持仓名义价值收取），已计入净值。"
        )

    if summary["sample_bars"] < 60:
        warnings.append(
            f"回测样本仅 {summary['sample_bars']} 根 K 线，夏普与年化收益的统计意义有限，结论只作演示。"
        )

    return {
        "strategy": "grid",
        "strategy_label": "网格交易",
        "params": asdict(params),
        "grid": {
            "lower": params.lower,
            "upper": params.upper,
            "count": params.grid_count,
            "step": step,
            "levels": levels,
        },
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "overlays": {"grid_levels": levels, "lines": []},
        "highlight": {
            "label": "触达网格",
            "value": f"{len(touched_levels)}/{params.grid_count + 1}",
            "help": "有成交的网格线数量 / 总网格线数量",
        },
        "warnings": warnings,
        "meta": {
            "engine": "grid-v2",
            "assumptions": [
                "限价单按网格线价格成交，未计滑点与挂单排队",
                "每条网格线最多持有一份仓位：该格卖出后才重新挂买单",
                "双边手续费各自按成交名义价值 × fee_rate 扣除",
                "只做多，不融资、不做空",
                "单根 K 线内按 open→低→高→close 路径模拟多次穿越",
                "合约模式按杠杆放大名义价值，最低价击穿强平价即损失该笔保证金",
            ],
            "bars_per_year": bars_per_year(params.interval),
            "mode": params.mode,
            "leverage": ledger.leverage,
        },
    }
