"""Backtesting package: causal bar engine, execution realism, metrics, walk-forward and Monte Carlo."""

from .engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    BacktestSignal,
    BacktestTrade,
)
from .execution_model import ActionKind, CorporateAction, ExecutionModel, PriceBasis
from .metrics import compute_metrics
from .overfitting import (
    CandidateEvaluation,
    RankedCandidate,
    RankingConfig,
    RankingResult,
    confirm_on_test,
    evaluate_candidates,
    rank_candidates,
)
from .monte_carlo import MonteCarloResult, monte_carlo_trade_analysis
from .walk_forward import (
    FoldResult,
    WalkForwardReport,
    WalkForwardWindow,
    make_windows,
    run_walk_forward,
    session_dates,
)

__all__ = [
    "CandidateEvaluation",
    "RankedCandidate",
    "RankingConfig",
    "RankingResult",
    "confirm_on_test",
    "evaluate_candidates",
    "rank_candidates",
    "ActionKind",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "BacktestSignal",
    "BacktestTrade",
    "CorporateAction",
    "ExecutionModel",
    "FoldResult",
    "MonteCarloResult",
    "PriceBasis",
    "WalkForwardReport",
    "WalkForwardWindow",
    "compute_metrics",
    "make_windows",
    "monte_carlo_trade_analysis",
    "run_walk_forward",
    "session_dates",
]
