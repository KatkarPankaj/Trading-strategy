"""Overlay trade-history bias on an inner symbol scorer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from stockmarket.domain.scorer import SymbolScorer


@dataclass
class BiasOverlayScorer:
    inner: SymbolScorer
    bias_map: dict[str, float]

    def score(
        self, symbol: str, features: Mapping[str, float] | None = None
    ) -> float | None:
        return self.inner.score(symbol, features)

    def bias_for(self, symbol: str) -> float:
        return float(self.bias_map.get(str(symbol), 0.0) or 0.0)

    def is_available(self) -> bool:
        return bool(getattr(self.inner, "is_available", lambda: False)())

    def supports_ml_scoring(self) -> bool:
        return bool(getattr(self.inner, "supports_ml_scoring", lambda: False)())

    def can_train(self) -> bool:
        return bool(getattr(self.inner, "can_train", lambda: False)())

    def train(self, watchlist: list[str], **kwargs):
        return self.inner.train(watchlist, **kwargs)

    def model_path(self):
        return getattr(self.inner, "model_path", lambda: None)()

    def batch_scores(self, symbols: tuple[str, ...]) -> dict[str, float]:
        fn = getattr(self.inner, "batch_scores", None)
        return fn(symbols) if callable(fn) else {}
