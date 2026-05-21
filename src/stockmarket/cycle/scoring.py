"""Effective buy/sell score adjustment (rule + ML + symbol bias)."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from stockmarket.domain.scorer import SymbolScorer


def apply_effective_scores(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    scorer: SymbolScorer,
    *,
    ml_enabled: bool,
    batch_ml_scores: Callable[[tuple[str, ...], float, float], dict[str, float]]
    | None = None,
    state_mtime: float = 0.0,
    model_mtime: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    buy_out = buy_df.copy() if not buy_df.empty else buy_df
    sell_out = sell_df.copy() if not sell_df.empty else sell_df
    sell_exit_out = sell_exit_df.copy() if not sell_exit_df.empty else sell_exit_df

    if not buy_out.empty:
        buy_out["symbol_bias"] = buy_out["symbol"].map(
            lambda s: float(scorer.bias_for(str(s)))
        )
    if not sell_out.empty:
        sell_out["symbol_bias"] = sell_out["symbol"].map(
            lambda s: float(scorer.bias_for(str(s)))
        )
    if not sell_exit_out.empty:
        sell_exit_out["symbol_bias"] = sell_exit_out["symbol"].map(
            lambda s: float(scorer.bias_for(str(s)))
        )

    if not buy_out.empty:
        rule_buy_score = pd.to_numeric(
            buy_out["buy_score"], errors="coerce"
        ).fillna(0.0)
        buy_bias = pd.to_numeric(
            buy_out["symbol_bias"], errors="coerce"
        ).fillna(0.0)

        ml_active = ml_enabled and bool(
            getattr(scorer, "supports_ml_scoring", lambda: False)()
        )
        if ml_active:
            buy_symbols = tuple(
                sorted({str(s) for s in buy_out["symbol"].astype(str).tolist()})
            )
            if batch_ml_scores is not None:
                ml_scores = batch_ml_scores(
                    buy_symbols, state_mtime, model_mtime
                )
            else:
                ml_scores = getattr(scorer, "batch_scores", lambda _: {})(
                    buy_symbols
                )
            buy_out["ml_quality_score"] = buy_out["symbol"].map(
                lambda s: float(ml_scores.get(str(s), 0.5))
            )
            ml_score = pd.to_numeric(
                buy_out["ml_quality_score"], errors="coerce"
            ).fillna(0.5)

            blended_buy_score = (0.65 * rule_buy_score) + (0.35 * (ml_score * 100.0))
            cap_series = pd.Series(92.0, index=blended_buy_score.index)
            cap_series = cap_series.where(
                ~((rule_buy_score >= 90.0) & (ml_score >= 0.80)),
                97.0,
            )
            effective_buy_score = (blended_buy_score + (0.8 * buy_bias)).clip(
                lower=0.0
            )
            effective_buy_score = effective_buy_score.where(
                effective_buy_score <= cap_series, cap_series
            )
            buy_out["effective_buy_score"] = effective_buy_score.round(2)
        else:
            buy_out["effective_buy_score"] = (
                rule_buy_score + buy_bias
            ).clip(lower=0.0, upper=95.0).round(2)

    if not sell_out.empty:
        sell_out["effective_sell_score"] = (
            pd.to_numeric(sell_out["sell_score"], errors="coerce").fillna(0.0)
            + pd.to_numeric(sell_out["symbol_bias"], errors="coerce").fillna(0.0)
        ).clip(lower=0.0, upper=95.0).round(2)

    if not sell_exit_out.empty:
        sell_exit_out["effective_sell_score"] = (
            pd.to_numeric(sell_exit_out["sell_score"], errors="coerce").fillna(
                0.0
            )
            + pd.to_numeric(sell_exit_out["symbol_bias"], errors="coerce").fillna(
                0.0
            )
        ).clip(lower=0.0, upper=95.0).round(2)

    return buy_out, sell_out, sell_exit_out
