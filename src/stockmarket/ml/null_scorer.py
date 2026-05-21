"""No-op symbol scorer for tests and when ML is disabled."""

from __future__ import annotations

from typing import Any, Mapping


class NullSymbolScorer:
    def score(
        self, symbol: str, features: Mapping[str, float] | None = None
    ) -> float | None:
        return None

    def bias_for(self, symbol: str) -> float:
        return 0.0

    def is_available(self) -> bool:
        return False

    def supports_ml_scoring(self) -> bool:
        return False

    def can_train(self) -> bool:
        return False

    def train(
        self,
        watchlist: list[str],
        *,
        use_historical_data: bool = True,
        historical_days: int = 60,
    ) -> dict[str, Any]:
        return {"status": "skipped", "reason": "ml scorer disabled"}

    def model_path(self) -> None:
        return None

    def batch_scores(self, symbols: tuple[str, ...]) -> dict[str, float]:
        return {}
