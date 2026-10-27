"""新闻 RSS 解析单测：结构提取、去重排序、坏 XML 容错。"""

from __future__ import annotations

import pytest

from core.news import merge_news, parse_rss

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Demo Feed</title>
    <item>
      <title>Bitcoin breaks resistance</title>
      <link>https://example.com/a</link>
      <description>&lt;p&gt;BTC &amp;amp; ETH &lt;b&gt;surge&lt;/b&gt; on ETF inflows&lt;/p&gt;</description>
      <pubDate>Mon, 26 Oct 2026 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Funding rates turn positive</title>
      <link>https://example.com/b</link>
      <description>Leverage is building up</description>
      <pubDate>Mon, 26 Oct 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>No date item</title>
      <link>https://example.com/c</link>
      <description>still valid</description>
    </item>
  </channel>
</rss>"""


def test_parse_rss_extracts_fields_and_cleans_html() -> None:
    items = parse_rss(SAMPLE_RSS, "Demo")
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "Bitcoin breaks resistance"
    assert first["link"] == "https://example.com/a"
    assert first["source"] == "Demo"
    assert "<" not in first["summary"]
    assert "BTC" in first["summary"]
    assert first["published_ts"] is not None


def test_parse_rss_handles_missing_date_and_bad_xml() -> None:
    items = parse_rss(SAMPLE_RSS, "Demo")
    no_date = next(item for item in items if item["title"] == "No date item")
    assert no_date["published_ts"] is None
    assert parse_rss("<rss><broken>", "Demo") == []
    assert parse_rss("", "Demo") == []


def test_merge_news_dedupes_sorts_and_limits() -> None:
    items = parse_rss(SAMPLE_RSS, "Demo") + parse_rss(SAMPLE_RSS, "Demo2")
    merged = merge_news(items, limit=2)
    assert len(merged) == 2
    # 最新的在前
    assert merged[0]["title"] == "Funding rates turn positive"
    titles = [item["title"] for item in merged]
    assert len(titles) == len(set(titles))
