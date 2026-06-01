"""Optimization package — trade history, features, reports."""

from .features import (
    MIN_TRAIN_TRADES_DEFAULT,
    MODEL_FEATURE_COLUMNS,
    QUALITY_THRESHOLD_DEFAULT,
    benchmark_symbol_for_cfg,
    enrich_trade_features,
    run_intelligent_optimization,
    run_intelligent_optimization_from_log,
)
from .reports import OptimizationReport, export_optimization_report
from .trade_history import (
    collect_clean_closed_trades,
    collect_clean_closed_trades_from_log,
    load_closed_trades,
    load_closed_trades_from_log,
)

__all__ = [
    "MIN_TRAIN_TRADES_DEFAULT",
    "MODEL_FEATURE_COLUMNS",
    "QUALITY_THRESHOLD_DEFAULT",
    "OptimizationReport",
    "benchmark_symbol_for_cfg",
    "collect_clean_closed_trades",
    "collect_clean_closed_trades_from_log",
    "enrich_trade_features",
    "export_optimization_report",
    "load_closed_trades",
    "load_closed_trades_from_log",
    "run_intelligent_optimization",
    "run_intelligent_optimization_from_log",
]
