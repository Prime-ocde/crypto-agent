"""FastAPI 入口：行情服务 + 回测 REST 接口。

接口清单：
- GET  /api/health       服务健康状态（含 WS 连接、重连次数、数据新鲜度）
- GET  /api/config       前端启动所需的默认参数（交易对、周期、费率）
- GET  /api/latest_data  最近 N 根 K 线 + 最新价
- POST /api/backtest     用服务端缓存跑一次网格回测（前端也可以本地跑 core/，两条路都通）
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.config import APP_VERSION, get_settings
from backend.external import get_news, get_sentiment, warmup as warmup_external
from backend.feeds import FeedManager
from backend.store import CandleStore, make_key
from core.aggregate import aggregate_candles
from core.strategy_registry import catalog as strategy_catalog
from core.strategy_registry import run_backtest as run_strategy_backtest
from core.strategy_registry import shared_params as strategy_shared_params
from core.timeframe import DERIVED_INTERVALS, SUPPORTED_INTERVALS, base_interval, interval_ms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("grid.backend.main")

# 派生周期 → 需要多少根基础 K 线才能合成一根
_DERIVED_RATIO = {"1y": 12}


def _resolve_candles(
    store: CandleStore,
    symbol: str,
    interval: str,
    limit: int,
) -> tuple[list[dict], str]:
    """取出目标周期的 K 线；派生周期（1y）从基础周期（1M）现场聚合。

    Returns:
        (candles, base_interval)：base_interval 用于判断数据新鲜度与数据源。
    """
    if interval in DERIVED_INTERVALS:
        base = base_interval(interval)
        ratio = _DERIVED_RATIO.get(interval, 12)
        base_candles = store.candles(make_key(symbol, base), min(1000, limit * ratio + ratio))
        return aggregate_candles(base_candles, interval)[-limit:], base
    return store.candles(make_key(symbol, interval), limit), interval


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    store = CandleStore(maxlen=settings.kline_limit)
    manager = FeedManager(settings, store)

    app.state.settings = settings
    app.state.store = store
    app.state.manager = manager
    app.state.started_at = time.time()

    logger.info(
        "启动：mode=%s symbols=%s intervals=%s",
        settings.run_mode,
        ",".join(settings.symbols),
        ",".join(settings.intervals),
    )
    await manager.start()
    # 后台预热情绪/新闻，避免首个页面请求卡在第三方超时上
    app.state.warmup_task = asyncio.create_task(asyncio.to_thread(warmup_external, settings))
    try:
        yield
    finally:
        logger.info("关停行情连接…")
        task = getattr(app.state, "warmup_task", None)
        if task is not None:
            task.cancel()
        await manager.stop()


app = FastAPI(
    title="Crypto Grid Quant · 模拟量化交易后端",
    version=APP_VERSION,
    description="Binance 公共行情 + 网格策略回测（纯模拟，不接真实资金）",
    lifespan=lifespan,
)

# 演示场景：Streamlit Cloud 与后端不同域，必须放开 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BacktestRequest(BaseModel):
    strategy: str = "grid"
    symbol: str | None = None
    interval: str | None = None
    limit: int = Field(default=500, ge=2, le=1000)
    # 通用参数袋：数值参数 + 交易模式字符串（spot / futures）
    params: dict[str, Any] = Field(default_factory=dict)
    # 以下为网格策略的兼容字段（老调用方仍可只传这些）
    lower: float | None = None
    upper: float | None = None
    grid_count: int | None = None
    initial_capital: float | None = None
    fee_rate: float | None = None
    liquidate_at_end: bool = False


@app.get("/")
def root() -> dict:
    return {
        "service": "crypto-grid-quant-backend",
        "version": APP_VERSION,
        "docs": "/docs",
        "endpoints": [
            "/api/health",
            "/api/config",
            "/api/latest_data",
            "/api/backtest",
            "/api/sentiment",
            "/api/news",
        ],
        "disclaimer": "纯模拟（Paper Trading），不接真实资金，不构成投资建议。",
    }


@app.get("/api/health")
def health(request: Request) -> dict:
    return request.app.state.manager.health()


@app.get("/api/config")
def config(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "version": APP_VERSION,
        "mode": settings.run_mode,
        "symbols": list(settings.symbols),
        "intervals": list(settings.intervals),
        "supported_intervals": list(SUPPORTED_INTERVALS),
        "default_symbol": settings.symbols[0],
        "default_interval": settings.intervals[0],
        "fee_rate": settings.fee_rate,
        "initial_capital": settings.initial_capital,
        "kline_limit": settings.kline_limit,
        "stream_keys": [f"{symbol}@{interval}" for symbol, interval in settings.stream_keys],
        "default_strategy": "grid",
        "strategies": strategy_catalog(),
        "shared_params": strategy_shared_params(),
        "trading_modes": ["spot", "futures"],
        "default_mode": "spot",
        "futures_fee_rate": 0.0005,
    }


@app.get("/api/latest_data")
def latest_data(
    request: Request,
    symbol: str | None = Query(default=None, description="交易对，如 BTCUSDT"),
    interval: str | None = Query(default=None, description="周期，如 1m / 5m"),
    limit: int = Query(default=300, ge=1, le=1000, description="返回最近多少根 K 线"),
) -> dict:
    settings = request.app.state.settings
    store: CandleStore = request.app.state.store
    manager: FeedManager = request.app.state.manager

    symbol = (symbol or settings.symbols[0]).upper()
    interval = interval or settings.intervals[0]
    key = make_key(symbol, interval)

    if key not in settings.available_keys:
        raise HTTPException(
            status_code=404,
            detail=(
                f"未订阅 {symbol}@{interval}。可用组合："
                + ", ".join(f"{s}@{i}" for s, i in settings.available_keys)
            ),
        )

    candles, base = _resolve_candles(store, symbol, interval, limit)
    if not candles:
        raise HTTPException(
            status_code=503,
            detail=f"{symbol}@{interval} 行情尚未就绪（基础周期 {base}），请稍后重试",
        )

    base_key = make_key(symbol, base)
    updated_at = store.last_update(base_key) or time.time()
    age = max(0.0, time.time() - updated_at)
    stale_threshold = max(3.0, interval_ms(base) / 1000.0 * 3)
    source = manager.source_of(base_key)
    source_label = source if base == interval else f"{source}（{base} 聚合）"

    return {
        "symbol": symbol,
        "interval": interval,
        "base_interval": base,
        "price": float(candles[-1]["c"]),
        "candles": candles,
        "count": len(candles),
        "source": source,
        "source_label": source_label,
        "mode": settings.run_mode,
        "stale": age > stale_threshold,
        "age_s": round(age, 2),
        "updated_at": datetime.fromtimestamp(updated_at, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "server_time": _iso_now(),
    }


@app.post("/api/backtest")
def backtest(payload: BacktestRequest, request: Request) -> dict:
    settings = request.app.state.settings
    store: CandleStore = request.app.state.store
    manager: FeedManager = request.app.state.manager

    symbol = (payload.symbol or settings.symbols[0]).upper()
    interval = payload.interval or settings.intervals[0]
    key = make_key(symbol, interval)

    candles, base = _resolve_candles(store, symbol, interval, payload.limit)
    if len(candles) < 2:
        raise HTTPException(
            status_code=404,
            detail=f"{symbol}@{interval} 缓存 K 线不足（基础周期 {base}），无法回测",
        )

    # 合并参数：params 袋优先，其次老字段，最后是服务端默认值
    params: dict[str, float] = dict(payload.params)
    for legacy_name in ("lower", "upper", "grid_count", "initial_capital", "fee_rate"):
        legacy_value = getattr(payload, legacy_name)
        if legacy_value is not None:
            params.setdefault(legacy_name, legacy_value)
    params.setdefault("initial_capital", settings.initial_capital)
    params["interval"] = interval
    # 手续费不强行注入：交给 registry 按模式取默认值（现货 0.1% / 合约 0.05%）
    if payload.strategy == "grid":
        params["liquidate_at_end"] = payload.liquidate_at_end

    try:
        result = run_strategy_backtest(payload.strategy, candles, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["symbol"] = symbol
    result["interval"] = interval
    result["data_source"] = manager.source_of(key)
    return result


@app.get("/api/sentiment")
def sentiment(request: Request, symbol: str | None = Query(default=None)) -> dict:
    """市场情绪：恐惧贪婪指数 + 合约资金费 + 多空比 → FOMO 分数。"""
    settings = request.app.state.settings
    return get_sentiment(settings, (symbol or settings.symbols[0]).upper())


@app.get("/api/news")
def news(request: Request, limit: int = Query(default=8, ge=1, le=30)) -> dict:
    """主要新闻（RSS 聚合），失败时返回明确标注的离线示例。"""
    settings = request.app.state.settings
    return get_news(settings, limit)


if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run("backend.main:app", host=cfg.backend_host, port=cfg.backend_port, reload=False)
