"""Aggregated cycle services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

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
    scorer: Any = None
