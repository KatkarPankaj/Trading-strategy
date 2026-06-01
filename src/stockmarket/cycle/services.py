"""Aggregated cycle services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from stockmarket.domain.scorer import SymbolScorer
from stockmarket.ml.null_scorer import NullSymbolScorer
from stockmarket.persistence.paper_repo import PaperRepo

from .entry.cooldown import DefaultCooldownPolicy
from .entry.idle_fallback import DefaultIdleFallbackPolicy
from .entry.sizing import DefaultPositionSizer
from .ports import (
    Broker,
    Clock,
    CooldownPolicy,
    HistoryQuery,
    IdleFallbackPolicy,
    PositionSizer,
    PriceRefresh,
    SignalSource,
)


@dataclass
class Services:
    clock: Clock
    broker: Broker
    signals: SignalSource
    history: HistoryQuery
    repo: PaperRepo
    prices: PriceRefresh
    charges_fn: Callable[[str, float], float]
    cooldown: CooldownPolicy = field(default_factory=DefaultCooldownPolicy)
    sizer: PositionSizer = field(default_factory=DefaultPositionSizer)
    idle_fallback: IdleFallbackPolicy = field(default_factory=DefaultIdleFallbackPolicy)
    scorer: SymbolScorer = field(default_factory=NullSymbolScorer)
    ml_enabled: bool = False
    batch_ml_scores: Callable[[tuple[str, ...], float | str, float], dict[str, float]] | None = (
        None
    )
    state_mtime: float | str = 0.0
    model_mtime: float = 0.0
