"""Validated, instrument-scoped research evidence for deterministic aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

RESEARCH_COMPONENTS = frozenset({
    "volume",
    "momentum",
    "sector",
    "news",
    "fundamental",
    "history",
})


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    """A bounded research score with explicit instrument, source and observation time."""

    instrument_id: str
    component: str
    score: float
    observed_at: datetime
    source: str
    history_trades: int = 0

    def __post_init__(self) -> None:
        for name in ("instrument_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.component, str) or self.component not in RESEARCH_COMPONENTS:
            raise ValueError(
                f"component must be one of {sorted(RESEARCH_COMPONENTS)}")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) \
                or not isfinite(self.score) or not -1 <= self.score <= 1:
            raise ValueError("score must be finite and between -1 and 1")
        if not isinstance(self.observed_at, datetime) \
                or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime")
        if isinstance(self.history_trades, bool) or not isinstance(self.history_trades, int) \
                or self.history_trades < 0:
            raise ValueError("history_trades must be a non-negative integer")
