"""Trading cycle context."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from stockmarket.domain.types import CycleSettings, DailyCounters, PaperState

from .ports import RankedSignals


@dataclass
class CycleContext:
    now: datetime
    today: str
    settings: CycleSettings
    state: PaperState
    counters: DailyCounters
    signals: RankedSignals

    same_cycle_exited: set[str] = field(default_factory=set)
    same_cycle_entered: set[str] = field(default_factory=set)
    entries_today: int = 0
    open_positions: int = 0

    actions: list[str] = field(default_factory=list)
    halt_cycle: bool = False
    halt_reason: str | None = None
