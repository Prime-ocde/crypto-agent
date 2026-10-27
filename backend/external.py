"""外部数据服务：恐惧贪婪指数、合约资金费、多空比、新闻 RSS。

设计原则（和行情一样的三级兜底）：
1. 优先实时抓取；
2. 失败时用上一次成功的缓存（stale-while-error）；
3. 再失败就返回**明确标注的离线示例**，绝不让页面白屏。

所有外部请求都带短超时（3s/6s）与 5 分钟缓存，避免把页面拖死或把对方打爆。
`RUN_MODE=mock` 时直接返回确定性模拟数据，不发起任何网络请求（单测/教室断网）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from backend.config import Settings
from core.news import merge_news, parse_rss
from core.sentiment import composite_sentiment

logger = logging.getLogger("grid.backend.external")

_UA = {"User-Agent": "crypto-grid-quant/0.1 (course project; read-only)"}
_TIMEOUT = (3.0, 6.0)
_TTL_SECONDS = 300.0

_NEWS_FEEDS: tuple[tuple[str, str], ...] = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
)

_CACHE: dict[str, tuple[float, Any]] = {}
_FAILED: dict[str, float] = {}
_LOCK = threading.Lock()
_FAILURE_TTL_SECONDS = 60.0


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _cached(key: str, loader: Callable[[], Any]) -> Any:
    """带 TTL 的缓存；抓取失败时退回上一次成功的值，并做 60s 负缓存避免反复超时。"""
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(key)
        failed_at = _FAILED.get(key)
    if entry and now - entry[0] < _TTL_SECONDS:
        return entry[1]
    if failed_at is not None and now - failed_at < _FAILURE_TTL_SECONDS:
        return entry[1] if entry else None
    try:
        value = loader()
    except Exception as exc:  # noqa: BLE001 - 外部依赖，统一降级
        logger.warning("外部数据抓取失败 %s: %s", key, exc)
        with _LOCK:
            _FAILED[key] = now
        return entry[1] if entry else None
    if value is not None:
        with _LOCK:
            _CACHE[key] = (now, value)
            _FAILED.pop(key, None)
    return value


# ---------------------------------------------------------------- 情绪
def _mock_fear_greed() -> dict:
    """确定性模拟：同一周内数值稳定，避免每次刷新乱跳。"""
    today = datetime.now(tz=timezone.utc).date().toordinal()
    history = []
    for offset in range(29, -1, -1):
        value = 32 + ((today // 7 + (29 - offset) * 3) % 58)
        history.append({"t": int((time.time() - offset * 86400) * 1000), "value": float(value)})
    return {"value": history[-1]["value"], "history": history}


def fetch_fear_greed() -> dict:
    response = requests.get(
        "https://api.alternative.me/fng/",
        params={"limit": 30, "format": "json"},
        timeout=_TIMEOUT,
        headers=_UA,
    )
    response.raise_for_status()
    payload = response.json().get("data") or []
    history = [
        {"t": int(item["timestamp"]) * 1000, "value": float(item["value"])}
        for item in reversed(payload)
        if item.get("value") is not None
    ]
    if not history:
        raise ValueError("恐惧贪婪指数返回为空")
    return {"value": history[-1]["value"], "history": history}


def fetch_funding_rate(symbol: str) -> float:
    response = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": symbol},
        timeout=_TIMEOUT,
        headers=_UA,
    )
    response.raise_for_status()
    payload = response.json()
    return float(payload["lastFundingRate"])


def fetch_long_short_ratio(symbol: str) -> float:
    response = requests.get(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        params={"symbol": symbol, "period": "1h", "limit": 1},
        timeout=_TIMEOUT,
        headers=_UA,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload:
        raise ValueError("多空比返回为空")
    return float(payload[-1]["longShortRatio"])


def get_sentiment(settings: Settings, symbol: str) -> dict:
    if settings.is_mock:
        fear_greed = _mock_fear_greed()
        funding_rate = 0.00012
        long_short_ratio = 1.35
        offline = True
        partial = False
        source = "Mock 模式（离线示例）"
    else:
        fear_greed = _cached("fear_greed", fetch_fear_greed)
        funding_rate = _cached(
            f"funding:{symbol}", lambda: fetch_funding_rate(symbol)
        )
        long_short_ratio = _cached(
            f"long_short:{symbol}", lambda: fetch_long_short_ratio(symbol)
        )
        offline = fear_greed is None or funding_rate is None or long_short_ratio is None
        fg_live = fear_greed is not None
        funding_live = funding_rate is not None
        ratio_live = long_short_ratio is not None

        live_parts: list[str] = []
        fallback_parts: list[str] = []
        if fear_greed is None:
            fear_greed = _mock_fear_greed()
            fallback_parts.append("恐惧贪婪=离线示例")
        else:
            live_parts.append("alternative.me")
        if funding_rate is None:
            funding_rate = 0.0001
            fallback_parts.append("资金费=默认值")
        else:
            live_parts.append("Binance 资金费")
        if long_short_ratio is None:
            long_short_ratio = 1.1
            fallback_parts.append("多空比=默认值")
        else:
            live_parts.append("Binance 多空比")
        source = " + ".join(live_parts + fallback_parts)
        # 主指标（恐惧贪婪）拿到就算"部分可用"，只缺合约指标时降级为 partial 而不是整块离线
        offline = not fg_live
        partial = fg_live and not (funding_live and ratio_live)

    result = composite_sentiment(
        fear_greed=fear_greed["value"],
        funding_rate=funding_rate,
        long_short_ratio=long_short_ratio,
    )
    result.update(
        {
            "symbol": symbol,
            "history": fear_greed.get("history", []),
            "offline": offline,
            "partial": partial,
            "source": source,
            "updated_at": _iso_now(),
        }
    )
    return result


# ---------------------------------------------------------------- 新闻
def _fallback_news(limit: int) -> list[dict]:
    now = int(time.time() * 1000)
    samples = [
        "离线示例：BTC 现货 ETF 单周净流入回升（示例数据，非实时新闻）",
        "离线示例：永续合约资金费率转正，多头情绪回暖（示例数据）",
        "离线示例：以太坊升级测试网进度更新（示例数据）",
        "离线示例：交易所储备金审计报告发布（示例数据）",
        "离线示例：宏观数据公布前，加密市场波动率抬升（示例数据）",
    ]
    return [
        {
            "title": title,
            "link": "https://www.binance.com/zh-CN/square",
            "summary": "后端无法访问新闻源时展示的占位内容，用于保证课堂演示不白屏。",
            "source": "离线示例",
            "published_ts": now - index * 3_600_000,
            "published_at": datetime.fromtimestamp(
                (now - index * 3_600_000) / 1000, tz=timezone.utc
            ).astimezone().isoformat(timespec="minutes"),
        }
        for index, title in enumerate(samples[:limit])
    ]


def _load_feeds() -> list[dict]:
    items: list[dict] = []
    for source, url in _NEWS_FEEDS:
        try:
            response = requests.get(url, timeout=_TIMEOUT, headers=_UA)
            response.raise_for_status()
            items.extend(parse_rss(response.text, source))
        except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其它源
            logger.warning("新闻源失败 %s: %s", source, exc)
    if not items:
        raise ValueError("所有新闻源均不可用")
    return items


def get_news(settings: Settings, limit: int = 8) -> dict:
    if settings.is_mock:
        return {
            "items": _fallback_news(limit),
            "offline": True,
            "source": "离线示例（Mock 模式）",
            "updated_at": _iso_now(),
        }

    items = _cached("news", _load_feeds)
    if not items:
        return {
            "items": _fallback_news(limit),
            "offline": True,
            "source": "离线示例（新闻源不可达）",
            "updated_at": _iso_now(),
        }
    return {
        "items": merge_news(items, limit),
        "offline": False,
        "source": "CoinDesk / Cointelegraph RSS",
        "updated_at": _iso_now(),
    }


def warmup(settings: Settings) -> None:
    """启动时后台预热外部数据，避免第一个页面请求卡在第三方超时上。"""
    if settings.is_mock:
        return
    get_sentiment(settings, settings.symbols[0])
    get_news(settings, 8)
