"""Market-learning scorer adapters."""

from __future__ import annotations

import os
from typing import Any, Callable

from stockmarket.domain.scorer import SymbolScorer

from .bias import BiasOverlayScorer
from .null_scorer import NullSymbolScorer
from .sklearn_scorer import SklearnSymbolScorer

__all__ = [
    "BiasOverlayScorer",
    "NullSymbolScorer",
    "SklearnSymbolScorer",
    "build_scorer",
    "with_bias_map",
]


def build_scorer(
    *,
    market: str = "NSE",
    trade_log_provider: Callable[[], list[Any]] | None = None,
) -> SymbolScorer:
    """Factory: null when disabled or sklearn unavailable; else sklearn adapter."""
    if os.environ.get("DISABLE_ML_SCORER") == "1":
        return NullSymbolScorer()
    scorer = SklearnSymbolScorer(
        market=market,
        trade_log_provider=trade_log_provider,
    )
    if not scorer.can_train():
        return NullSymbolScorer()
    return scorer


def with_bias_map(scorer: SymbolScorer, bias_map: dict[str, float]) -> SymbolScorer:
    if not bias_map:
        return scorer
    return BiasOverlayScorer(scorer, bias_map)
