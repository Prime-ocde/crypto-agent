"""新闻 RSS 解析（纯函数）。

不依赖任何付费 API / Key：直接解析交易所与媒体公开的 RSS。
把网络请求隔离在 `backend/external.py`，这里只负责"把 XML 变成结构化新闻"，
方便用固定字符串写单测。
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _clean_text(raw: str | None, limit: int = 180) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def parse_rss(xml_text: str, source: str) -> list[dict]:
    """解析 RSS 2.0，返回 [{title, link, summary, source, published_ts, published_at}]。"""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict] = []
    for node in root.iter("item"):
        title = _clean_text(node.findtext("title"), limit=160)
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        summary = _clean_text(node.findtext("description"))
        published_ts: int | None = None
        published_at: str | None = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                moment = parsedate_to_datetime(raw_date)
                published_ts = int(moment.timestamp() * 1000)
                published_at = moment.isoformat(timespec="minutes")
            except (TypeError, ValueError, OverflowError):
                published_ts = None
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": source,
                "published_ts": published_ts,
                "published_at": published_at,
            }
        )
    return items


def merge_news(items: list[dict], limit: int = 8) -> list[dict]:
    """按标题去重、按发布时间倒序、截断。没有时间的排在最后。"""
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        key = item["title"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda item: item.get("published_ts") or 0, reverse=True)
    return unique[:limit]
