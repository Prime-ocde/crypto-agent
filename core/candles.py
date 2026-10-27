"""K 线输入规范化：所有策略引擎共用同一套校验，保证"同一份数据、同一套口径"。

支持输入：
- `pandas.DataFrame`（列名可以是 t/o/h/l/c 或 time/open/high/low/close）
- `list[dict]`
- 任何带 `to_dict("records")` 的对象

输出统一为按时间升序、去重后的 `list[{"t": ms, "o","h","l","c","v": float}]`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

_MAX_MS = 10_000_000_000


def to_ms(value: Any) -> int:
    """把秒/毫秒时间戳统一成毫秒。"""
    ts = int(float(value))
    return ts * 1000 if ts < _MAX_MS else ts


def iso_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_candles(candles: Any) -> list[dict]:
    if candles is None:
        raise ValueError("K 线数据为空，无法回测")

    rows: list[dict] = []
    if hasattr(candles, "to_dict"):  # pandas.DataFrame
        candidates = candles.to_dict("records")
    elif isinstance(candles, Sequence) and not isinstance(candles, (str, bytes)):
        candidates = list(candles)
    else:
        raise TypeError("candles 必须是 DataFrame 或 dict 序列")

    alias = {
        "t": ("t", "time", "open_time", "timestamp", "ts", "T"),
        "o": ("o", "open", "O"),
        "h": ("h", "high", "H"),
        "l": ("l", "low", "L"),
        "c": ("c", "close", "C"),
    }

    for item in candidates:
        if not isinstance(item, Mapping):
            raise TypeError("每根 K 线必须是 dict（或 DataFrame 的行）")
        row: dict[str, Any] = {}
        for target, keys in alias.items():
            for key in keys:
                if key in item and item[key] is not None:
                    row[target] = item[key]
                    break
            else:
                raise ValueError(f"K 线缺少字段 {target!r}（可接受别名：{', '.join(keys)}）")
        volume = item.get("v", item.get("volume", item.get("V", 0.0)))
        row["v"] = float(volume or 0.0)
        row["t"] = to_ms(row["t"])
        for key in ("o", "h", "l", "c"):
            row[key] = float(row[key])
            if row[key] <= 0:
                raise ValueError(f"K 线价格必须为正数，收到 {row[key]}")
        # 容忍个别数据源影线字段不规范，保证内部高 >= 低
        row["h"] = max(row["h"], row["o"], row["c"], row["l"])
        row["l"] = min(row["l"], row["o"], row["c"], row["h"])
        rows.append(row)

    if not rows:
        raise ValueError("K 线数据为空，无法回测")

    rows.sort(key=lambda r: r["t"])
    if len(rows) < 2:
        raise ValueError("回测至少需要 2 根 K 线")

    # 去重：同一时间戳只保留最后一根（通常是同一根未收盘 K 线的更新）
    deduped: list[dict] = []
    for row in rows:
        if deduped and row["t"] == deduped[-1]["t"]:
            deduped[-1] = row
        else:
            deduped.append(row)
    if len(deduped) < 2:
        raise ValueError("去重后 K 线不足 2 根，无法回测")
    return deduped
