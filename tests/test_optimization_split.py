"""Optimizer package imports and report shape parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockmarket.config import TradingConfig
from stockmarket.optimization import (
    benchmark_symbol_for_cfg,
    collect_clean_closed_trades,
    export_optimization_report,
    run_intelligent_optimization,
)

FIXTURE_TRADES = Path(__file__).parent / "fixtures" / "golden" / "backtest_trades_synthetic.csv"
GOLDEN_KEYS = Path(__file__).parent / "fixtures" / "golden" / "optimization_report_keys.json"


def test_benchmark_symbol_for_cfg_nse():
    cfg = TradingConfig(market_timezone="Asia/Kolkata")
    assert benchmark_symbol_for_cfg(cfg) == "^NSEI"


def test_collect_trades_via_package():
    if not FIXTURE_TRADES.exists():
        pytest.skip("backtest golden trades fixture missing")
    trades, _ = collect_clean_closed_trades(FIXTURE_TRADES)
    assert not trades.empty


def test_optimization_report_artifact_keys(tmp_path, monkeypatch):
    if not FIXTURE_TRADES.exists():
        pytest.skip("backtest golden trades fixture missing")

    monkeypatch.setattr(
        "stockmarket.optimization.features.fetch_intraday_data",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    cfg = TradingConfig()
    report = run_intelligent_optimization(FIXTURE_TRADES, cfg, lookback_trades=50)
    paths = export_optimization_report(report, tmp_path, "test_opt")
    expected = {
        "recommendations_json",
        "symbol_scores_csv",
        "feature_scores_csv",
        "model_feature_importance_csv",
        "walkforward_comparison_csv",
        "clean_closed_trades_csv",
        "enriched_trades_csv",
    }
    assert set(paths.keys()) == expected
    for p in paths.values():
        assert p.exists()

    if not GOLDEN_KEYS.exists():
        GOLDEN_KEYS.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary_keys": sorted(report.summary.keys()),
            "recommendation_count": len(report.recommendations),
            "symbol_score_rows": int(len(report.symbol_scores)),
            "feature_score_rows": int(len(report.feature_scores)),
        }
        GOLDEN_KEYS.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    golden = json.loads(GOLDEN_KEYS.read_text(encoding="utf-8"))
    assert sorted(report.summary.keys()) == golden["summary_keys"]
    assert len(report.recommendations) == golden["recommendation_count"]
    assert len(report.symbol_scores) == golden["symbol_score_rows"]
    assert len(report.feature_scores) == golden["feature_score_rows"]
