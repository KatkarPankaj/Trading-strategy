"""Time-series-safe rolling and expanding walk-forward validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from itertools import product
from math import isfinite
from typing import Any, Literal, Mapping, Sequence

import pandas as pd

from ..backtest import BacktestResult, run_backtest, validate_ohlcv_data
from ..config import TradingConfig
from ..core.market_session import MarketSession


ParameterValue = int | float | str
AllowedParameter = Literal[
    "opening_range_minutes",
    "stop_loss_pct",
    "take_profit_pct",
    "volume_spike_threshold",
    "volume_ma_window",
    "vwap_price_source",
]


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_sessions: int
    test_sessions: int
    step_sessions: int
    mode: Literal["rolling", "expanding"] = "rolling"
    gap_sessions: int = 0
    min_train_trades: int = 1
    periods_per_year: int = 252
    annual_risk_free_rate: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "train_sessions", "test_sessions", "step_sessions",
            "min_train_trades", "periods_per_year",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.gap_sessions, bool) or not isinstance(self.gap_sessions, int):
            raise TypeError("gap_sessions must be an integer")
        if self.gap_sessions < 0:
            raise ValueError("gap_sessions must be non-negative")
        if self.step_sessions < self.test_sessions:
            raise ValueError(
                "step_sessions must be >= test_sessions to avoid OOS overlap")
        if self.mode not in ("rolling", "expanding"):
            raise ValueError("mode must be 'rolling' or 'expanding'")
        if (
            isinstance(self.annual_risk_free_rate, bool)
            or not isinstance(self.annual_risk_free_rate, (int, float))
            or not isfinite(self.annual_risk_free_rate)
        ):
            raise ValueError("annual_risk_free_rate must be finite")
        if self.annual_risk_free_rate <= -1:
            raise ValueError("annual_risk_free_rate must be greater than -1")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold: int
    status: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    selected_parameters: dict[str, ParameterValue] | None
    train_metrics: dict[str, Any] | None
    test_metrics: dict[str, Any] | None
    test_trades: pd.DataFrame
    test_equity_curve: pd.DataFrame
    reason: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "status": self.status,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "selected_parameters": self.selected_parameters,
            "train_metrics": self.train_metrics,
            "test_metrics": self.test_metrics,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[WalkForwardFold, ...]
    aggregate_oos_metrics: dict[str, Any]
    oos_trades: pd.DataFrame
    oos_equity_curve: pd.DataFrame
    config: WalkForwardConfig

    def folds_frame(self) -> pd.DataFrame:
        return pd.DataFrame([fold.as_record() for fold in self.folds])


def walk_forward_validate(
    df: pd.DataFrame,
    base_config: TradingConfig,
    validation: WalkForwardConfig,
    *,
    parameter_grid: Mapping[AllowedParameter,
                            Sequence[ParameterValue]] | None = None,
    commission_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
) -> WalkForwardResult:
    """Select parameters on each train window, then evaluate the following OOS block."""
    data = validate_ohlcv_data(df)
    if not isinstance(validation, WalkForwardConfig):
        raise TypeError("validation must be a WalkForwardConfig")
    if not isinstance(base_config, TradingConfig):
        raise TypeError("base_config must be a TradingConfig")
    for name, value in (
        ("commission_multiplier", commission_multiplier),
        ("slippage_multiplier", slippage_multiplier),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
        if value < 0 or not isfinite(value):
            raise ValueError(f"{name} must be finite and non-negative")

    grid = _normalize_parameter_grid(base_config, parameter_grid)
    session = MarketSession.from_config(base_config)
    local_index = data.index.tz_convert(session.timezone)
    session_dates = sorted(set(local_index.date))
    minimum_dates = (
        validation.train_sessions
        + validation.gap_sessions
        + validation.test_sessions
    )
    if len(session_dates) < minimum_dates:
        raise ValueError(
            f"Need at least {minimum_dates} observed sessions for one walk-forward fold; "
            f"received {len(session_dates)}"
        )
    folds: list[WalkForwardFold] = []
    oos_trade_frames: list[pd.DataFrame] = []
    oos_equity_frames: list[pd.DataFrame] = []
    out_of_sample_capital = float(base_config.starting_capital)

    test_start_position = validation.train_sessions + validation.gap_sessions
    fold_number = 1
    while test_start_position < len(session_dates):
        test_end_position = min(
            test_start_position + validation.test_sessions,
            len(session_dates),
        )
        train_end_position = test_start_position - validation.gap_sessions
        train_start_position = (
            max(0, train_end_position - validation.train_sessions)
            if validation.mode == "rolling"
            else 0
        )
        train_dates = session_dates[train_start_position:train_end_position]
        test_dates = session_dates[test_start_position:test_end_position]
        if not test_dates or len(train_dates) < validation.train_sessions:
            break

        train_start = train_dates[0]
        train_end = train_dates[-1]
        test_start = test_dates[0]
        test_end = test_dates[-1]
        local_dates = pd.Index(local_index.date)
        train_mask = local_dates.isin(train_dates)
        test_mask = local_dates.isin(test_dates)
        train_data = data.loc[train_mask]
        test_data = data.loc[test_mask]

        selected = _select_training_parameters(
            train_data,
            base_config,
            grid,
            validation,
            commission_multiplier,
            slippage_multiplier,
        )
        if selected is None:
            folds.append(
                WalkForwardFold(
                    fold=fold_number,
                    status="skipped",
                    train_start=str(train_start),
                    train_end=str(train_end),
                    test_start=str(test_start),
                    test_end=str(test_end),
                    selected_parameters=None,
                    train_metrics=None,
                    test_metrics=None,
                    test_trades=pd.DataFrame(),
                    test_equity_curve=pd.DataFrame(),
                    reason="No training configuration met min_train_trades",
                )
            )
        else:
            selected_config, train_result = selected
            test_config = replace(
                selected_config,
                starting_capital=out_of_sample_capital,
            )
            warmup_count = max(0, test_config.volume_ma_window - 1)
            warmup_data = train_data.tail(
                warmup_count) if warmup_count else train_data.iloc[:0]
            test_input = pd.concat([warmup_data, test_data]).sort_index()
            test_result = run_backtest(
                test_input,
                test_config,
                periods_per_year=validation.periods_per_year,
                annual_risk_free_rate=validation.annual_risk_free_rate,
                commission_multiplier=commission_multiplier,
                slippage_multiplier=slippage_multiplier,
                evaluation_start=test_start,
                evaluation_end=test_end,
            )
            if not test_result.equity_curve.empty:
                out_of_sample_capital = float(
                    test_result.equity_curve["equity"].iloc[-1])
                curve = test_result.equity_curve.copy()
                curve["fold"] = fold_number
                oos_equity_frames.append(curve)
            trades = test_result.trades.copy()
            if not trades.empty:
                trades["fold"] = fold_number
                oos_trade_frames.append(trades)
            folds.append(
                WalkForwardFold(
                    fold=fold_number,
                    status="evaluated",
                    train_start=str(train_start),
                    train_end=str(train_end),
                    test_start=str(test_start),
                    test_end=str(test_end),
                    selected_parameters=_parameter_values(selected_config),
                    train_metrics=train_result.summary,
                    test_metrics=test_result.summary,
                    test_trades=trades,
                    test_equity_curve=test_result.equity_curve,
                )
            )

        fold_number += 1
        test_start_position += validation.step_sessions

    oos_trades = (
        pd.concat(oos_trade_frames, ignore_index=True)
        if oos_trade_frames else pd.DataFrame()
    )
    oos_equity_curve = (
        pd.concat(oos_equity_frames, ignore_index=True)
        if oos_equity_frames else pd.DataFrame(
            columns=["timestamp", "equity",
                     "realized_capital", "position_open", "fold"]
        )
    )
    if not oos_equity_curve.empty:
        oos_equity_curve = oos_equity_curve.sort_values(
            "timestamp").reset_index(drop=True)
    aggregate = _aggregate_oos_metrics(
        oos_trades,
        oos_equity_curve.drop(columns=["fold"], errors="ignore"),
        starting_capital=float(base_config.starting_capital),
        validation=validation,
    )
    return WalkForwardResult(
        folds=tuple(folds),
        aggregate_oos_metrics=aggregate,
        oos_trades=oos_trades,
        oos_equity_curve=oos_equity_curve,
        config=validation,
    )


def _normalize_parameter_grid(
    base: TradingConfig,
    grid: Mapping[AllowedParameter, Sequence[ParameterValue]] | None,
) -> dict[str, tuple[ParameterValue, ...]]:
    allowed = {
        "opening_range_minutes",
        "stop_loss_pct",
        "take_profit_pct",
        "volume_spike_threshold",
        "volume_ma_window",
        "vwap_price_source",
    }
    if grid is None:
        return {name: (getattr(base, name),) for name in sorted(allowed)}
    unknown = set(grid).difference(allowed)
    if unknown:
        raise ValueError(
            f"Unsupported walk-forward parameters: {sorted(unknown)}")
    normalized: dict[str, tuple[ParameterValue, ...]] = {}
    for name in sorted(allowed):
        values = tuple(grid[name]) if name in grid else (getattr(base, name),)
        if not values:
            raise ValueError(f"Parameter grid for {name} must not be empty")
        normalized[name] = tuple(dict.fromkeys(values))
    return normalized


def _select_training_parameters(
    train_data: pd.DataFrame,
    base: TradingConfig,
    grid: Mapping[str, tuple[ParameterValue, ...]],
    validation: WalkForwardConfig,
    commission_multiplier: float,
    slippage_multiplier: float,
) -> tuple[TradingConfig, BacktestResult] | None:
    names = tuple(grid)
    candidates: list[tuple[TradingConfig, BacktestResult]] = []
    for values in product(*(grid[name] for name in names)):
        candidate = replace(base, **dict(zip(names, values)))
        result = run_backtest(
            train_data,
            candidate,
            periods_per_year=validation.periods_per_year,
            annual_risk_free_rate=validation.annual_risk_free_rate,
            commission_multiplier=commission_multiplier,
            slippage_multiplier=slippage_multiplier,
        )
        if result.summary["total_trades"] >= validation.min_train_trades:
            candidates.append((candidate, result))
    if not candidates:
        return None

    # Rank equally across distinct risk/performance views; never use test data.
    metric_order = (
        "sharpe_ratio", "sortino_ratio", "profit_factor",
        "maximum_drawdown", "expectancy", "total_trades",
    )
    rank_totals = [0.0] * len(candidates)
    for metric in metric_order:
        order = sorted(
            range(len(candidates)),
            key=lambda idx: _selection_value(
                candidates[idx][1].summary.get(metric)),
            reverse=True,
        )
        for rank, index in enumerate(order):
            rank_totals[index] += rank
    best = min(range(len(candidates)), key=lambda idx: (rank_totals[idx], idx))
    return candidates[best]


def _selection_value(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return float("-inf")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    return numeric if isfinite(numeric) else float("-inf")


def _parameter_values(config: TradingConfig) -> dict[str, ParameterValue]:
    return {
        "opening_range_minutes": config.opening_range_minutes,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "volume_spike_threshold": config.volume_spike_threshold,
        "volume_ma_window": config.volume_ma_window,
        "vwap_price_source": config.vwap_price_source,
    }


def _aggregate_oos_metrics(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    *,
    starting_capital: float,
    validation: WalkForwardConfig,
) -> dict[str, Any]:
    if equity_curve.empty:
        return {
            "total_trades": 0,
            "total_return": None,
            "status": "no_evaluated_oos_folds",
        }
    from .statistics import calculate_performance_metrics

    metrics = calculate_performance_metrics(
        trades,
        equity_curve,
        starting_capital=starting_capital,
        periods_per_year=validation.periods_per_year,
        annual_risk_free_rate=validation.annual_risk_free_rate,
    )
    metrics["status"] = "evaluated"
    return metrics
