"""策略注册表：统一参数描述 + 统一入口 + 统一结果契约。

前端/API 只需要认识三个东西：
- `catalog()`   → 有哪些策略、每个策略要哪些参数（含默认值/范围/说明）
- `run_backtest(strategy, candles, params)` → 跑一次回测，返回统一结构
- `STRATEGIES`  → 策略元数据

新增策略 = 写一个引擎 + 在这里注册，前端零改动。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.candles import normalize_candles
from core.execution import VALID_DIRECTIONS, VALID_MODES, default_fee_rate
from core.grid_strategy import GridParams, run_grid_backtest
from core.martingale import MartingaleParams, run_martingale_backtest
from core.trend import TrendParams, run_trend_backtest


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: str = "float"  # float | int
    default: float = 0.0
    min: float = 0.0
    max: float = 1_000_000.0
    step: float = 1.0
    format: str = "%.4f"
    help: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StrategySpec:
    key: str
    label: str
    icon: str
    tagline: str
    description: str
    params: tuple[ParamSpec, ...] = ()

    def defaults(self) -> dict[str, float]:
        return {param.key: param.default for param in self.params}

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "icon": self.icon,
            "tagline": self.tagline,
            "description": self.description,
            "params": [param.as_dict() for param in self.params],
        }


STRATEGIES: dict[str, StrategySpec] = {
    "grid": StrategySpec(
        key="grid",
        label="网格交易",
        icon="🕸️",
        tagline="震荡行情吃价差，单边行情扛单",
        description="在区间内等距挂网格，下跌买、上涨卖，赚的是波动而不是方向。",
        params=(
            ParamSpec("grid_count", "网格数量", "int", 20, 2, 100, 1, "%d", "格数越多单格利润越薄；间距必须大于双边手续费"),
        ),
    ),
    "martingale": StrategySpec(
        key="martingale",
        label="马丁格尔",
        icon="🎲",
        tagline="高胜率、尾部风险极大",
        description="下跌按倍数加仓摊平成本，反弹到均价止盈。胜率极高，但一次单边下跌可能吞掉全部利润。",
        params=(
            ParamSpec("initial_order_usdt", "首次建仓 (USDT)", "float", 300.0, 10.0, 50_000.0, 50.0, "%.2f", "第一层的下单金额"),
            ParamSpec("drop_pct", "加仓触发跌幅", "float", 0.005, 0.001, 0.2, 0.001, "%.3f", "相对上一次成交价下跌多少触发加仓；1 分钟级别建议 0.3%~1%"),
            ParamSpec("multiplier", "加仓倍数", "float", 2.0, 1.0, 4.0, 0.1, "%.1f", "每层仓位 = 上一层 × 倍数；2 = 经典翻倍"),
            ParamSpec("take_profit_pct", "止盈比例", "float", 0.003, 0.001, 0.1, 0.001, "%.3f", "相对持仓均价上涨多少全部止盈"),
            ParamSpec("max_entries", "最大层数", "int", 5, 2, 12, 1, "%d", "含首次建仓；层数越多资金需求是几何级数增长"),
        ),
    ),
    "trend": StrategySpec(
        key="trend",
        label="趋势跟踪",
        icon="📈",
        tagline="双均线交叉，赚趋势、亏震荡",
        description="快线上穿慢线买入、下穿卖出，配合止损止盈。信号在下一根开盘执行，严格无未来函数。",
        params=(
            ParamSpec("fast_ma", "快线周期", "int", 10, 2, 100, 1, "%d", "短周期均线"),
            ParamSpec("slow_ma", "慢线周期", "int", 30, 3, 300, 1, "%d", "长周期均线，必须大于快线"),
            ParamSpec("position_pct", "仓位比例", "float", 1.0, 0.1, 1.0, 0.05, "%.2f", "每次开仓使用可用资金的比例"),
            ParamSpec("stop_loss_pct", "止损比例", "float", 0.02, 0.0, 0.2, 0.005, "%.3f", "0 = 不设止损"),
            ParamSpec("take_profit_pct", "止盈比例", "float", 0.0, 0.0, 0.2, 0.005, "%.3f", "0 = 不设止盈，等死叉离场"),
        ),
    ),
}

STRATEGY_KEYS: tuple[str, ...] = tuple(STRATEGIES)

# 三个策略共享的执行层参数（模式 / 杠杆 / 资金费率），前端有独立控件
SHARED_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("mode", "交易模式", "choice", 0, 0, 1, 0, "%s", "spot = 现货；futures = 合约（多头永续）"),
    ParamSpec("leverage", "杠杆倍数", "float", 1.0, 1.0, 50.0, 1.0, "%.0f", "仅合约模式生效；杠杆越高强平线越近"),
    ParamSpec(
        "funding_rate_8h",
        "资金费率 / 8h",
        "float",
        0.0001,
        0.0,
        0.005,
        0.0001,
        "%.4f",
        "合约模式按持仓名义价值每 8 小时收取（多头支付）",
    ),
)


def shared_params() -> list[dict]:
    return [param.as_dict() for param in SHARED_PARAMS]


def get_spec(strategy: str) -> StrategySpec:
    try:
        return STRATEGIES[strategy]
    except KeyError:
        raise ValueError(
            f"未知策略：{strategy!r}，可选：{', '.join(STRATEGY_KEYS)}"
        ) from None


def catalog() -> list[dict]:
    return [spec.as_dict() for spec in STRATEGIES.values()]


def _number(merged: dict, key: str, default: float) -> float:
    value = merged.get(key)
    if value is None:
        return default
    return float(value)


def run_backtest(strategy: str, candles: Any, params: dict[str, Any] | None = None) -> dict:
    """统一入口：根据策略名分发到对应引擎，并注入策略元数据。"""
    spec = get_spec(strategy)
    rows = normalize_candles(candles)
    merged: dict[str, Any] = {**spec.defaults(), **(params or {})}

    interval = str(merged.get("interval") or "1m")
    capital = max(_number(merged, "initial_capital", 10_000.0), 1.0)

    mode = str(merged.get("mode") or "spot").lower()
    if mode not in VALID_MODES:
        raise ValueError(f"交易模式必须是 {' / '.join(VALID_MODES)}，收到 {mode!r}")
    leverage = _number(merged, "leverage", 1.0) if mode == "futures" else 1.0
    funding_rate = _number(merged, "funding_rate_8h", 0.0) if mode == "futures" else 0.0
    fee_value = merged.get("fee_rate")
    fee_rate = float(fee_value) if fee_value is not None else default_fee_rate(mode)
    direction = "long_only"

    if strategy == "grid":
        last_price = rows[-1]["c"]
        lower = _number(merged, "lower", last_price * 0.97)
        upper = _number(merged, "upper", last_price * 1.03)
        grid_params = GridParams(
            lower=lower,
            upper=upper,
            grid_count=int(merged.get("grid_count") or 20),
            initial_capital=capital,
            fee_rate=fee_rate,
            interval=interval,
            mode=mode,
            leverage=leverage,
            funding_rate_8h=funding_rate,
        )
        result = run_grid_backtest(
            rows, grid_params, liquidate_at_end=bool(merged.get("liquidate_at_end", False))
        )
    elif strategy == "martingale":
        martingale_params = MartingaleParams(
            initial_order_usdt=_number(merged, "initial_order_usdt", 300.0),
            drop_pct=_number(merged, "drop_pct", 0.005),
            multiplier=_number(merged, "multiplier", 2.0),
            take_profit_pct=_number(merged, "take_profit_pct", 0.003),
            max_entries=int(merged.get("max_entries") or 5),
            initial_capital=capital,
            fee_rate=fee_rate,
            interval=interval,
            mode=mode,
            leverage=leverage,
            funding_rate_8h=funding_rate,
        )
        result = run_martingale_backtest(rows, martingale_params)
    elif strategy == "trend":
        direction = merged.get("direction")
        if direction is None:
            # 合约模式默认开启双向；现货只能做多
            direction = "long_short" if mode == "futures" else "long_only"
        direction = str(direction)
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"交易方向必须是 {' / '.join(VALID_DIRECTIONS)}，收到 {direction!r}"
            )
        trend_params = TrendParams(
            fast_ma=int(merged.get("fast_ma") or 10),
            slow_ma=int(merged.get("slow_ma") or 30),
            position_pct=_number(merged, "position_pct", 1.0),
            stop_loss_pct=_number(merged, "stop_loss_pct", 0.02),
            take_profit_pct=_number(merged, "take_profit_pct", 0.0),
            initial_capital=capital,
            fee_rate=fee_rate,
            interval=interval,
            mode=mode,
            leverage=leverage,
            funding_rate_8h=funding_rate,
            direction=direction,
        )
        result = run_trend_backtest(rows, trend_params)
    else:  # pragma: no cover - get_spec 已经拦截
        raise ValueError(f"未知策略：{strategy!r}")

    result["strategy"] = spec.key
    result["strategy_label"] = spec.label
    result["strategy_icon"] = spec.icon
    result["tagline"] = spec.tagline
    result["execution"] = {
        "mode": mode,
        "leverage": leverage,
        "fee_rate": fee_rate,
        "funding_rate_8h": funding_rate,
        "direction": direction,
    }
    return result
