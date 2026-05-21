"""Sklearn-backed symbol scorer delegating to market_learning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

_SKLEARN_MOD: Any | None = None


def _market_learning():
    global _SKLEARN_MOD
    if _SKLEARN_MOD is None:
        try:
            from stockmarket import market_learning as mod

            _SKLEARN_MOD = mod
        except Exception:
            _SKLEARN_MOD = False
    return _SKLEARN_MOD if _SKLEARN_MOD is not False else None


class SklearnSymbolScorer:
    """Lazy-loads market_learning; owns model path resolution under outputs/."""

    def __init__(
        self,
        state_file: Path | None = None,
        model_path: Path | None = None,
    ):
        self._state_file = Path(state_file or "outputs/simple_paper_state.json")
        self._model_path = model_path
        self._get_score = None
        self._resolve_path = None
        self._train_fn = None

    def _ensure(self) -> bool:
        mod = _market_learning()
        if mod is None:
            return False
        if self._get_score is None:
            self._get_score = mod.get_symbol_quality_score
            self._resolve_path = mod.resolve_learning_model_path
            self._train_fn = mod.train_market_learning_model
        return True

    def _resolved_model_path(self) -> Path:
        if self._model_path is not None:
            return self._model_path
        if not self._ensure():
            return Path("outputs") / "market_learning_model.pkl"
        return self._resolve_path(self._state_file)

    def model_path(self) -> Path | None:
        if not self._ensure():
            return None
        return self._resolved_model_path()

    def is_available(self) -> bool:
        return self._ensure()

    def supports_ml_scoring(self) -> bool:
        return self._ensure()

    def can_train(self) -> bool:
        return self._ensure()

    def score(
        self, symbol: str, features: Mapping[str, float] | None = None
    ) -> float | None:
        if not self._ensure():
            return None
        try:
            return float(self._get_score(symbol, self._state_file))
        except Exception:
            return None

    def bias_for(self, symbol: str) -> float:
        return 0.0

    def batch_scores(self, symbols: tuple[str, ...]) -> dict[str, float]:
        if not self._ensure():
            return {}
        scores: dict[str, float] = {}
        for sym in symbols:
            val = self.score(sym)
            if val is not None:
                scores[sym] = val
        return scores

    def train(
        self,
        watchlist: list[str],
        *,
        use_historical_data: bool = True,
        historical_days: int = 60,
    ) -> dict[str, Any]:
        if not self._ensure():
            return {"status": "skipped", "reason": "market_learning unavailable"}
        return self._train_fn(
            self._state_file,
            watchlist,
            use_historical_data=use_historical_data,
            historical_days=historical_days,
        )
