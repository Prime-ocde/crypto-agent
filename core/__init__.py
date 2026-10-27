"""核心包：与框架无关的网格回测引擎与绩效指标计算。"""

from core.grid_strategy import GridParams, run_grid_backtest  # noqa: F401

__all__ = ["GridParams", "run_grid_backtest"]
