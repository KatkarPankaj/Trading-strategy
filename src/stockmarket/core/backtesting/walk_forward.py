"""Rolling TRAIN / VALIDATE / TEST walk-forward evaluation; test windows are never used for selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..models import Instrument
from .engine import BacktestEngine, BacktestResult, SignalFn


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    index: int
    train: tuple[date, date]
    validate: tuple[date, date]
    test: tuple[date, date]


def make_windows(
    session_dates: Sequence[date],
    *,
    train: int,
    validate: int,
    test: int,
    step: int | None = None,
    embargo: int = 0,
) -> list[WalkForwardWindow]:
    """Sizes are in sessions; `embargo` sessions separate adjacent segments to limit leakage."""
    if min(train, validate, test) < 1 or embargo < 0 or (step is not None and step < 1):
        raise ValueError(
            "train, validate, test and step must be positive; embargo non-negative")
    days = sorted(set(session_dates))
    step = step or test
    span = train + embargo + validate + embargo + test
    windows = []
    for n, s in enumerate(range(0, len(days) - span + 1, step)):
        a = s + train
        b = a + embargo
        c = b + validate
        d = c + embargo
        windows.append(WalkForwardWindow(
            n, (days[s], days[a - 1]), (days[b], days[c - 1]), (days[d], days[d + test - 1])))
    return windows


def session_dates(bars: Mapping[str, pd.DataFrame], instruments: Mapping[str, Instrument]) -> list[date]:
    out: set[date] = set()
    for iid, df in bars.items():
        out.update(ts.date()
                   for ts in df.index.tz_convert(instruments[iid].timezone))
    return sorted(out)


@dataclass(frozen=True, slots=True)
class FoldResult:
    window: WalkForwardWindow
    parameters: Mapping[str, Any] | None
    train_metrics: dict[str, Any] | None
    validate_metrics: dict[str, Any] | None
    test: BacktestResult | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    folds: tuple[FoldResult, ...]
    oos_trade_pnls: tuple[float, ...]

    @property
    def completed_folds(self) -> int:
        return sum(1 for f in self.folds if f.test is not None)


def default_score(metrics: dict[str, Any]) -> float:
    sharpe = metrics.get("sharpe")
    return float("-inf") if sharpe is None else float(sharpe)


def run_walk_forward(
    engine: BacktestEngine,
    instruments: Mapping[str, Instrument],
    bars: Mapping[str, pd.DataFrame],
    strategy_factory: Callable[[Mapping[str, Any]], SignalFn],
    parameter_grid: Sequence[Mapping[str, Any]],
    windows: Sequence[WalkForwardWindow],
    *,
    min_trades: int = 10,
    score: Callable[[dict[str, Any]], float] = default_score,
    **run_kwargs: Any,
) -> WalkForwardReport:
    """Per fold: fit candidates on TRAIN, rank on VALIDATE, evaluate the winner once on TEST."""
    if not parameter_grid:
        raise ValueError("parameter_grid must not be empty")
    folds: list[FoldResult] = []
    oos: list[float] = []
    for w in windows:
        best: tuple[float, Mapping[str, Any], dict, dict] | None = None
        for params in parameter_grid:
            fn = strategy_factory(params)
            try:
                tr = engine.run(instruments, bars, fn,
                                start=w.train[0], end=w.train[1], **run_kwargs)
                va = engine.run(
                    instruments, bars, fn, start=w.validate[0], end=w.validate[1], **run_kwargs)
            except ValueError:
                continue
            if tr.metrics.get("trades", 0) < min_trades or va.metrics.get("trades", 0) < 1:
                continue
            candidate = score(va.metrics)
            if best is None or candidate > best[0]:
                best = (candidate, params, tr.metrics, va.metrics)
        if best is None or best[0] == float("-inf"):
            folds.append(FoldResult(w, None, None, None, None,
                         "no candidate met min_trades on train/validate"))
            continue
        try:
            test = engine.run(instruments, bars, strategy_factory(best[1]),
                              start=w.test[0], end=w.test[1], **run_kwargs)
        except ValueError as exc:
            folds.append(FoldResult(
                w, best[1], best[2], best[3], None, f"test window failed: {exc}"))
            continue
        oos.extend(t.net_pnl_base for t in test.trades)
        folds.append(FoldResult(w, best[1], best[2], best[3], test))
    return WalkForwardReport(tuple(folds), tuple(oos))
