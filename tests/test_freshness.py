"""行情新鲜度策略单测。"""

from __future__ import annotations

from core.freshness import usable


def test_recent_data_is_usable() -> None:
    assert usable(5.0, 60.0) is True
    assert usable(60.0, 60.0) is True


def test_old_or_unknown_data_is_not_usable() -> None:
    assert usable(61.0, 60.0) is False
    assert usable(None, 60.0) is False
    assert usable(-1.0, 60.0) is False
    assert usable("bad", 60.0) is False


def test_zero_window_never_usable_except_exact_zero() -> None:
    assert usable(0.0, 0.0) is True
    assert usable(1.0, 0.0) is False
