"""Phase 07: SymbolScorer port, adapters, and effective-score parity."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stockmarket.cycle.scoring import apply_effective_scores
from stockmarket.ml import build_scorer, with_bias_map
from stockmarket.ml.null_scorer import NullSymbolScorer


def _sample_frames():
    buy_df = pd.DataFrame(
        [{"symbol": "AAA", "buy_score": 80.0, "sell_score": 0.0}]
    )
    sell_df = pd.DataFrame(
        [{"symbol": "BBB", "sell_score": 70.0}]
    )
    return buy_df, sell_df, pd.DataFrame()


def test_null_scorer_effective_buy_equals_rule_score():
    buy_df, sell_df, sell_exit_df = _sample_frames()
    scorer = NullSymbolScorer()
    out_buy, out_sell, _ = apply_effective_scores(
        buy_df,
        sell_df,
        sell_exit_df,
        scorer,
        ml_enabled=True,
    )
    assert float(out_buy.iloc[0]["effective_buy_score"]) == pytest.approx(80.0)
    assert float(out_sell.iloc[0]["effective_sell_score"]) == pytest.approx(70.0)


def test_bias_overlay_adds_to_effective_scores():
    buy_df, sell_df, sell_exit_df = _sample_frames()
    scorer = with_bias_map(NullSymbolScorer(), {"AAA": 2.5, "BBB": -1.0})
    out_buy, out_sell, _ = apply_effective_scores(
        buy_df,
        sell_df,
        sell_exit_df,
        scorer,
        ml_enabled=False,
    )
    assert float(out_buy.iloc[0]["effective_buy_score"]) == pytest.approx(82.5)
    assert float(out_sell.iloc[0]["effective_sell_score"]) == pytest.approx(69.0)


def test_build_scorer_respects_disable_env(monkeypatch):
    monkeypatch.setenv("DISABLE_ML_SCORER", "1")
    assert isinstance(build_scorer(), NullSymbolScorer)


def test_dashboard_apply_effective_scores_without_ml(monkeypatch):
    import dashboard_simple

    monkeypatch.setenv("DISABLE_ML_SCORER", "1")
    buy_df, sell_df, sell_exit_df = _sample_frames()
    out_buy, out_sell, _ = dashboard_simple._apply_effective_scores(
        buy_df,
        sell_df,
        sell_exit_df,
        {"AAA": 0.0},
    )
    assert float(out_buy.iloc[0]["effective_buy_score"]) == float(
        out_buy.iloc[0]["buy_score"]
    )
    assert float(out_sell.iloc[0]["effective_sell_score"]) == float(
        out_sell.iloc[0]["sell_score"]
    )


def test_null_scorer_bias_for_is_zero():
    assert NullSymbolScorer().bias_for("RELIANCE.NS") == 0.0
