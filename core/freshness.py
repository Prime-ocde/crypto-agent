"""行情新鲜度策略（纯函数）。

为什么单独抽出来：前端降级链有好几种"旧数据"（后端缓存、CSV 缓存、上次成功的一次请求），
它们的可用期不同，判定散落在 `frontend/app.py` 里会很难测。这里只回答一个问题：
**这份数据还能不能拿来顶一下？**
"""

from __future__ import annotations


def usable(age_s: float | None, max_age_s: float) -> bool:
    """数据年龄在可接受范围内 → True。

    `age_s` 为 None 表示"不知道多旧"，按不可用处理（宁可明确降级，也不假装新鲜）。
    """
    if age_s is None:
        return False
    try:
        age = float(age_s)
    except (TypeError, ValueError):
        return False
    if age < 0:
        return False
    return age <= max(float(max_age_s), 0.0)
