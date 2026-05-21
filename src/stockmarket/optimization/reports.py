from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class OptimizationReport:
    summary: dict[str, Any]
    recommendations: list[dict[str, Any]]
    symbol_scores: pd.DataFrame
    feature_scores: pd.DataFrame
    model_feature_importance: pd.DataFrame
    walkforward_comparison: pd.DataFrame
    trades: pd.DataFrame


def export_optimization_report(report: OptimizationReport, out_dir: str | Path, prefix: str) -> dict[str, Path]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    json_path = out_path / f"{prefix}_recommendations.json"
    symbols_path = out_path / f"{prefix}_symbol_scores.csv"
    features_path = out_path / f"{prefix}_feature_scores.csv"
    model_path = out_path / f"{prefix}_model_feature_importance.csv"
    walkforward_path = out_path / f"{prefix}_walkforward_comparison.csv"
    clean_trades_path = out_path / f"{prefix}_clean_closed_trades.csv"
    trades_path = out_path / f"{prefix}_enriched_trades.csv"

    payload = {
        "summary": report.summary,
        "recommendations": report.recommendations,
    }
    json_path.write_text(pd.Series(payload).to_json(
        indent=2), encoding="utf-8")
    report.symbol_scores.to_csv(symbols_path, index=False)
    report.feature_scores.to_csv(features_path, index=False)
    report.model_feature_importance.to_csv(model_path, index=False)
    report.walkforward_comparison.to_csv(walkforward_path, index=False)
    report.trades[[
        "trade_source",
        "symbol",
        "side",
        "entry_ts",
        "exit_ts",
        "qty",
        "entry_price",
        "exit_price",
        "net_pnl",
        "commission",
        "exit_reason",
        "holding_minutes",
        "return_pct",
        "is_win",
    ]].to_csv(clean_trades_path, index=False)
    report.trades.to_csv(trades_path, index=False)

    return {
        "recommendations_json": json_path,
        "symbol_scores_csv": symbols_path,
        "feature_scores_csv": features_path,
        "model_feature_importance_csv": model_path,
        "walkforward_comparison_csv": walkforward_path,
        "clean_closed_trades_csv": clean_trades_path,
        "enriched_trades_csv": trades_path,
    }
