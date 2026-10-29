"""后端配置：全部来自环境变量 / .env，代码里不写死任何地址与费率。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from core.timeframe import SUPPORTED_INTERVALS, required_intervals

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

# .env 优先从项目根目录加载（本地开发）；云端直接用平台注入的环境变量
load_dotenv(PROJECT_ROOT / ".env")

APP_VERSION = "0.1.0"
VALID_MODES = ("live", "mock")
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEFAULT_INTERVALS = ("1m", "5m", "1h", "1d", "1w", "1M", "1y")


def _raw(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _as_float(name: str, default: float) -> float:
    try:
        return float(_raw(name, str(default)))
    except ValueError:
        return default


def _as_int(name: str, default: int) -> int:
    try:
        return int(float(_raw(name, str(default))))
    except ValueError:
        return default


def _as_tuple(name: str, default: tuple[str, ...], *, upper: bool) -> tuple[str, ...]:
    raw = _raw(name)
    if not raw:
        return default
    items = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    normalised = tuple(item.upper() if upper else item for item in items)
    return normalised or default


def _as_intervals(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """周期必须**保留大小写**：`1m`（分钟）和 `1M`（月）是完全不同的周期。"""
    raw = _raw(name)
    if not raw:
        return default
    items = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    valid = tuple(item for item in items if item in SUPPORTED_INTERVALS)
    return valid or default


@dataclass(frozen=True)
class Settings:
    run_mode: str
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    kline_limit: int
    ws_url: str
    ws_fallbacks: tuple[str, ...]
    rest_url: str
    rest_fallback: str
    backoff_base: float
    backoff_max: float
    jitter_ratio: float
    idle_timeout: float
    rest_refresh_seconds: float
    fee_rate: float
    initial_capital: float
    mock_tick_seconds: float
    backend_host: str
    backend_port: int
    backend_base_url: str

    @property
    def stream_intervals(self) -> tuple[str, ...]:
        """需要真实订阅的周期（派生周期展开成基础周期，例如 1y → 1M）。"""
        return required_intervals(self.intervals)

    @property
    def stream_keys(self) -> tuple[tuple[str, str], ...]:
        """要订阅的 (symbol, interval) 组合（仅原生周期）。"""
        return tuple((symbol, interval) for symbol in self.symbols for interval in self.stream_intervals)

    @property
    def available_keys(self) -> tuple[tuple[str, str], ...]:
        """对外可查询的组合（含 1y 这类派生周期）。"""
        return tuple((symbol, interval) for symbol in self.symbols for interval in self.intervals)

    @property
    def is_mock(self) -> bool:
        return self.run_mode == "mock"


def get_settings() -> Settings:
    """每次调用都重新读环境变量：单测里改 env 立即生效，不需要清 import 缓存。"""
    mode = _raw("RUN_MODE", "live").lower()
    if mode not in VALID_MODES:
        mode = "live"

    intervals = _as_intervals("INTERVALS", DEFAULT_INTERVALS)

    symbols = _as_tuple("SYMBOLS", DEFAULT_SYMBOLS, upper=True) or DEFAULT_SYMBOLS
    single = _raw("SYMBOL", "").upper()
    if single and single not in symbols:
        symbols = (single, *symbols)

    limit = max(50, min(_as_int("KLINE_LIMIT", 1000), 5000))

    return Settings(
        run_mode=mode,
        symbols=symbols,
        intervals=intervals,
        kline_limit=limit,
        # 阶段二：行情统一走币安官方"仅公共行情"域名（更稳、无需 Key、不带下单能力）
        ws_url=_raw("BINANCE_WS_URL", "wss://data-stream.binance.vision/ws"),
        ws_fallbacks=_as_tuple(
            "BINANCE_WS_FALLBACKS",
            ("wss://stream.binance.com:9443/ws", "wss://stream.binance.com:443/ws"),
            upper=False,
        ),
        rest_url=_raw("BINANCE_REST_URL", "https://data-api.binance.vision").rstrip("/"),
        rest_fallback=_raw("BINANCE_REST_FALLBACK", "https://api.binance.com").rstrip("/"),
        backoff_base=max(0.2, _as_float("WS_BACKOFF_BASE", 1.0)),
        backoff_max=max(1.0, _as_float("WS_BACKOFF_MAX", 60.0)),
        jitter_ratio=min(max(_as_float("WS_BACKOFF_JITTER", 0.3), 0.0), 0.9),
        idle_timeout=max(5.0, _as_float("WS_IDLE_TIMEOUT", 30.0)),
        # WS 断开/假死时用 REST 补数据的间隔（秒）；0 表示关闭这条兜底
        rest_refresh_seconds=max(0.0, _as_float("REST_REFRESH_SECONDS", 30.0)),
        fee_rate=min(max(_as_float("FEE_RATE", 0.001), 0.0), 0.05),
        initial_capital=max(_as_float("INITIAL_CAPITAL", 10_000.0), 1.0),
        mock_tick_seconds=min(max(_as_float("MOCK_TICK_SECONDS", 0.4), 0.1), 5.0),
        backend_host=_raw("BACKEND_HOST", "0.0.0.0"),
        backend_port=_as_int("BACKEND_PORT", 8000),
        backend_base_url=_raw("BACKEND_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
    )
