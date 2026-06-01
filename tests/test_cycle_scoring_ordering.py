"""Cycle scoring: effective scores vs research-rank order."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stockmarket.cycle.ports import RankedSignals
from stockmarket.cycle.scoring import apply_effective_scores, apply_scorer_to_ranked_signals
from stockmarket.ml import with_bias_map
from stockmarket.ml.null_scorer import NullSymbolScorer


def test_bias_overlay_changes_effective_score_without_reordering_rows():
    buy_df = pd.DataFrame(
        [
            {"symbol": "AAA", "buy_score": 70.0, "research_buy_score": 90.0},
            {"symbol": "BBB", "buy_score": 90.0, "research_buy_score": 80.0},
        ]
    )
    scorer = with_bias_map(NullSymbolScorer(), {"AAA": 0.0, "BBB": 15.0})
    out_buy, _, _ = apply_effective_scores(
        buy_df, pd.DataFrame(), pd.DataFrame(), scorer, ml_enabled=False
    )
    assert out_buy["symbol"].tolist() == ["AAA", "BBB"]
    assert float(out_buy.iloc[0]["effective_buy_score"]) == pytest.approx(70.0)
    assert float(out_buy.iloc[1]["effective_buy_score"]) == pytest.approx(95.0)
    by_effective = out_buy.sort_values("effective_buy_score", ascending=False)
    assert by_effective.iloc[0]["symbol"] == "BBB"


def test_apply_scorer_to_ranked_signals_preserves_symbol_order():
    ranked = RankedSignals(
        buy_df=pd.DataFrame([{"symbol": "X", "buy_score": 60.0}]),
        sell_df=pd.DataFrame([{"symbol": "Y", "sell_score": 55.0}]),
        sell_exit_df=pd.DataFrame([{"symbol": "Z", "sell_score": 50.0}]),
    )
    scored = apply_scorer_to_ranked_signals(
        ranked,
        with_bias_map(NullSymbolScorer(), {"X": 1.0, "Y": -1.0, "Z": 2.0}),
        ml_enabled=False,
    )
    assert scored.buy_df["symbol"].tolist() == ["X"]
    assert float(scored.buy_df.iloc[0]["effective_buy_score"]) == pytest.approx(61.0)
    assert float(scored.sell_df.iloc[0]["effective_sell_score"]) == pytest.approx(54.0)
    assert float(scored.sell_exit_df.iloc[0]["effective_sell_score"]) == pytest.approx(52.0)
