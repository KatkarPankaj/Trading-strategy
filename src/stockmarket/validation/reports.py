"""Machine- and human-readable walk-forward/robustness report serialization."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from ..config import TradingConfig
from .robustness import RobustnessResult
from .walk_forward import WalkForwardResult


REPORT_VERSION = 1


def write_validation_report(
    output_directory: str | Path,
    *,
    symbol: str,
    config: TradingConfig,
    walk_forward: WalkForwardResult,
    robustness: RobustnessResult | None = None,
    data_source: str = "Yahoo Finance research data",
    data_summary: dict[str, Any] | None = None,
    report_id: str = "validation",
) -> dict[str, Path]:
    """Write one validation result as strict JSON, CSV rows, and Markdown."""
    if not isinstance(config, TradingConfig):
        raise TypeError("config must be a TradingConfig")
    if not isinstance(walk_forward, WalkForwardResult):
        raise TypeError("walk_forward must be a WalkForwardResult")
    if robustness is not None and not isinstance(robustness, RobustnessResult):
        raise TypeError("robustness must be a RobustnessResult or None")
    if not symbol.strip() or not report_id.strip():
        raise ValueError("symbol and report_id must be non-empty")

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"validation_{_safe_name(symbol)}_{_safe_name(report_id)}"
    paths = {
        "json": output / f"{stem}.json",
        "csv": output / f"{stem}.csv",
        "markdown": output / f"{stem}.md",
    }
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(
            f"Refusing to overwrite existing validation report: {existing[0]}"
        )

    document = _build_document(
        symbol=symbol,
        config=config,
        walk_forward=walk_forward,
        robustness=robustness,
        data_source=data_source,
        data_summary=data_summary,
    )
    safe_document = _json_safe(document)
    paths["json"].write_text(
        json.dumps(safe_document, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    _build_rows(walk_forward, robustness).to_csv(paths["csv"], index=False)
    paths["markdown"].write_text(
        _render_markdown(document), encoding="utf-8"
    )
    return paths


def _build_document(
    *,
    symbol: str,
    config: TradingConfig,
    walk_forward: WalkForwardResult,
    robustness: RobustnessResult | None,
    data_source: str,
    data_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "symbol": symbol,
        "data_source": data_source,
        "data_summary": data_summary or {},
        "config": asdict(config),
        "methodology": {
            "split_mode": walk_forward.config.mode,
            "train_sessions": walk_forward.config.train_sessions,
            "test_sessions": walk_forward.config.test_sessions,
            "step_sessions": walk_forward.config.step_sessions,
            "gap_sessions": walk_forward.config.gap_sessions,
            "selection_uses_test_data": False,
            "execution_policy": "next_observed_bar_open_stop_first",
            "sharpe_sortino_basis": "daily_closing_equity_returns",
            "exposure_basis": "fraction_of_observed_bars_with_position_open",
            "risk_free_rate_annual": walk_forward.config.annual_risk_free_rate,
            "periods_per_year": walk_forward.config.periods_per_year,
            "calendar_limitation": "Splits use observed session dates; no holiday calendar is available.",
        },
        "in_sample_selection": [
            {
                "fold": fold.fold,
                "status": fold.status,
                "train_start": fold.train_start,
                "train_end": fold.train_end,
                "selected_parameters": fold.selected_parameters,
                "metrics": fold.train_metrics,
            }
            for fold in walk_forward.folds
        ],
        "out_of_sample_evaluation": {
            "aggregate_metrics": walk_forward.aggregate_oos_metrics,
            "folds": [
                {
                    "fold": fold.fold,
                    "status": fold.status,
                    "train_start": fold.train_start,
                    "train_end": fold.train_end,
                    "test_start": fold.test_start,
                    "test_end": fold.test_end,
                    "metrics": fold.test_metrics,
                    "reason": fold.reason,
                }
                for fold in walk_forward.folds
            ],
        },
        "robustness": None if robustness is None else [
            {
                "scenario_id": result.scenario.scenario_id,
                "name": result.scenario.name,
                "parameters": asdict(result.scenario.config),
                "commission_multiplier": result.scenario.commission_multiplier,
                "slippage_multiplier": result.scenario.slippage_multiplier,
                "aggregate_oos_metrics": result.walk_forward.aggregate_oos_metrics,
                "folds": [
                    {
                        "fold": fold.fold,
                        "status": fold.status,
                        "test_start": fold.test_start,
                        "test_end": fold.test_end,
                        "metrics": fold.test_metrics,
                        "reason": fold.reason,
                    }
                    for fold in result.walk_forward.folds
                ],
            }
            for result in robustness.scenarios
        ],
    }


def _build_rows(
    walk_forward: WalkForwardResult,
    robustness: RobustnessResult | None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for fold in walk_forward.folds:
        rows.append(_fold_row("walk_forward", "selected_parameters", fold))
    if robustness is not None:
        for scenario_result in robustness.scenarios:
            for fold in scenario_result.walk_forward.folds:
                rows.append(
                    _fold_row(
                        "robustness",
                        scenario_result.scenario.scenario_id,
                        fold,
                        scenario_name=scenario_result.scenario.name,
                        commission_multiplier=scenario_result.scenario.commission_multiplier,
                        slippage_multiplier=scenario_result.scenario.slippage_multiplier,
                        parameters=_compact_json(
                            _scenario_config(scenario_result)),
                    )
                )
    return pd.DataFrame(rows)


def _fold_row(
    report_type: str,
    scenario_id: str,
    fold: Any,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "report_type": report_type,
        "scenario_id": scenario_id,
        "fold": fold.fold,
        "status": fold.status,
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "selected_parameters": _compact_json(fold.selected_parameters),
        "skip_reason": fold.reason,
    }
    row.update(extra)
    for prefix, metrics in (
        ("train", fold.train_metrics),
        ("oos", fold.test_metrics),
    ):
        for key, value in (metrics or {}).items():
            row[f"{prefix}_{key}"] = _json_safe(value)
    return row


def _scenario_config(result: Any) -> dict[str, Any]:
    return asdict(result.scenario.config)


def _render_markdown(document: dict[str, Any]) -> str:
    metrics = document["out_of_sample_evaluation"]["aggregate_metrics"]
    lines = [
        f"# Statistical Validation Report: {document['symbol']}",
        "",
        f"- Report version: {REPORT_VERSION}",
        f"- Data source: {document['data_source']}",
        f"- Observed data: {_display(document.get('data_summary', {}).get('observations'))} bars, "
        f"{_display(document.get('data_summary', {}).get('observed_sessions'))} sessions, "
        f"{_display(document.get('data_summary', {}).get('first_timestamp'))} to "
        f"{_display(document.get('data_summary', {}).get('last_timestamp'))}",
        f"- Execution policy: {document['methodology']['execution_policy']}",
        f"- Split mode: {document['methodology']['split_mode']}",
        f"- Selection uses OOS data: {document['methodology']['selection_uses_test_data']}",
        "",
        "## Out-of-sample aggregate",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    metric_keys = (
        "total_trades", "total_return", "win_rate", "profit_factor",
        "expectancy", "maximum_drawdown", "sharpe_ratio", "sortino_ratio",
        "average_win", "average_loss", "max_consecutive_wins",
        "max_consecutive_losses", "exposure", "status",
    )
    for key in metric_keys:
        if key in metrics:
            lines.append(f"| {key} | {_display(metrics[key])} |")
    lines.extend(["", "## Fold results", "", "| Fold | Status | Train | Test | OOS trades | OOS return | Reason |",
                 "|---:|---|---|---|---:|---:|---|"])
    for fold in document["out_of_sample_evaluation"]["folds"]:
        fold_metrics = fold.get("metrics") or {}
        lines.append(
            f"| {fold['fold']} | {fold['status']} | "
            f"{fold.get('train_start', '')}–{fold.get('train_end', '')} | "
            f"{fold.get('test_start', '')}–{fold.get('test_end', '')} | "
            f"{_display(fold_metrics.get('total_trades'))} | "
            f"{_display(fold_metrics.get('total_return'))} | "
            f"{fold.get('reason') or ''} |"
        )
    lines.extend([
        "",
        "## In-sample selection",
        "",
        "Parameters are selected using each fold's training interval only. The following metrics are not OOS results.",
        "",
        "| Fold | Status | Train | Selected parameters | IS return | IS Sharpe |",
        "|---:|---|---|---|---:|---:|",
    ])
    for fold in document["in_sample_selection"]:
        train_metrics = fold.get("metrics") or {}
        lines.append(
            f"| {fold['fold']} | {fold['status']} | "
            f"{fold.get('train_start', '')}–{fold.get('train_end', '')} | "
            f"{_display(_compact_json(fold.get('selected_parameters')))} | "
            f"{_display(train_metrics.get('total_return'))} | "
            f"{_display(train_metrics.get('sharpe_ratio'))} |"
        )
    lines.extend([
        "",
        "## Assumptions and limitations",
        "",
        "- This is historical research, not a profit guarantee or live-execution claim.",
        "- Intrabar ambiguity uses stop-first handling when both stop and target are crossed.",
        "- Observed session dates are used; no holiday or early-close calendar is available.",
        f"- Detected missing in-session bars: {_display(document.get('data_summary', {}).get('missing_in_session_bars'))} "
        "(counted only between observed bars on the same date; bars are not synthesized).",
        "- Undefined statistics are shown as `null` in JSON and `N/A` here.",
        "",
    ])
    if document.get("robustness") is not None:
        lines.extend(["## Robustness scenarios", "",
                     "| Scenario | Commission × | Slippage × | OOS trades | OOS return | Status |", "|---|---:|---:|---:|---:|---|"])
        for scenario in document["robustness"]:
            scenario_metrics = scenario.get("aggregate_oos_metrics") or {}
            lines.append(
                f"| {scenario['scenario_id']} | {scenario['commission_multiplier']} | "
                f"{scenario['slippage_multiplier']} | "
                f"{_display(scenario_metrics.get('total_trades'))} | "
                f"{_display(scenario_metrics.get('total_return'))} | "
                f"{scenario_metrics.get('status', 'unknown')} |"
            )
        lines.append("")
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return str(value)


def _compact_json(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(_json_safe(value), sort_keys=True, allow_nan=False)


def _display(value: Any) -> str:
    safe = _json_safe(value)
    return "N/A" if safe is None else str(safe)


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return normalized.strip("_") or "validation"
