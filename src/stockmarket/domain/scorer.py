"""Symbol quality scoring port (ML + optional trade-history bias)."""

from __future__ import annotations

from typing import Mapping, Protocol


class SymbolScorer(Protocol):
    """Score symbols for ranking; bias adjusts effective buy/sell scores."""

    def score(
        self, symbol: str, features: Mapping[str, float] | None = None
    ) -> float | None:
        """Return ML quality score in [0, 1], or None if unavailable."""
        ...

    def bias_for(self, symbol: str) -> float:
        """Additive bias applied to effective buy/sell scores; default 0.0."""
        ...
