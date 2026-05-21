"""Optimizer package split: shim imports and report shape parity."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pandas as pd
import pytest

from stockmarket.config import TradingConfig
from stockmarket.optimization import (
    collect_clean_closed_trades,
    export_optimization_report,
    run_intelligent_optimization,
)
from stockmarket.optimizer import (
    _benchmark_symbol_for_cfg,
    collect_clean_closed_trades as shim_collect,
    run_intelligent_optimization as shim_run,
)

FIXTURE_TRADES = Path(__file__).parent / "fixtures" / "golden" / "backtest_trades_synthetic.csv"
GOLDEN_KEYS = Path(__file__).parent / "fixtures" / "golden" / "optimization_report_keys.json"


def test_shim_exports_benchmark_helper():
    cfg = TradingConfig(market_timezone="Asia/Kolkata")
    assert _benchmark_symbol_for_cfg(cfg) == "^NSEI"


def test_shim_warns_on_import():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        import importlib
        import stockmarket.optimizer as opt

        importlib.reload(opt)
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_collect_trades_via_shim_and_package():
    if not FIXTURE_TRADES.exists():
        pytest.skip("backtest golden trades fixture missing")
    a, _ = collect_clean_closed_trades(FIXTURE_TRADES)
    b, _ = shim_collect(FIXTURE_TRADES)
    assert len(a) == len(b)
    assert set(a.columns) == set(b.columns)


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

    shim_report = shim_run(FIXTURE_TRADES, cfg, lookback_trades=50)
    assert shim_report.summary.keys() == report.summary.keys()
