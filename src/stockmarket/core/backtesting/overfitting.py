"""Anti-overfitting candidate ranking: out-of-sample, risk-adjusted, stable and parameter-robust."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from statistics import mean, pstdev
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..models import Instrument
from .engine import BacktestEngine, BacktestResult, SignalFn
from .walk_forward import WalkForwardWindow

COMPONENTS = (
    "oos_performance", "drawdown", "sharpe", "sortino",
    "profit_factor", "trade_count", "stability", "sensitivity",
)


def _default_weights() -> dict[str, float]:
    return {"oos_performance": 0.20, "drawdown": 0.15, "sharpe": 0.15, "sortino": 0.10,
            "profit_factor": 0.10, "trade_count": 0.10, "stability": 0.10, "sensitivity": 0.10}


@dataclass(frozen=True, slots=True)
class RankingConfig:
    weights: Mapping[str, float] = field(default_factory=_default_weights)
    min_oos_trades: int = 30
    max_oos_drawdown: float = 0.30
    min_positive_fold_share: float = 0.6
    multiple_testing_threshold: int = 20
    suspicious_sharpe: float = 3.0
    suspicious_profit_factor: float = 5.0
    max_is_oos_sharpe_ratio: float = 2.0

    def __post_init__(self) -> None:
        if set(self.weights) != set(COMPONENTS):
            raise ValueError(
                f"weights must define exactly: {', '.join(COMPONENTS)}")
        if any(not isfinite(w) or w < 0 for w in self.weights.values()) or sum(self.weights.values()) <= 0:
            raise ValueError(
                "weights must be finite, non-negative, with a positive sum")
        if self.min_oos_trades < 1 or not 0 < self.max_oos_drawdown <= 1:
            raise ValueError("invalid trade-count or drawdown limit")


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Aggregates for one parameter set; 'oos' figures come from windows not used to fit it."""

    parameters: Mapping[str, Any]
    is_sharpe: float | None
    oos_sharpe: float | None
    oos_sortino: float | None
    oos_profit_factor: float | None
    oos_max_drawdown: float
    oos_trades: int
    oos_total_return: float
    fold_returns: tuple[float, ...]
    # in-sample plus out-of-sample, used only to show what a PnL-only pick would be
    total_pnl: float


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    parameters: Mapping[str, Any]
    composite_score: float
    components: Mapping[str, float | None]
    warnings: tuple[str, ...]
    disqualified: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RankingResult:
    ranked: tuple[RankedCandidate, ...]
    selected: RankedCandidate | None
    highest_pnl_parameters: Mapping[str, Any] | None
    pnl_pick_differs: bool
    requires_review: bool  # True whenever the selection carries warnings; never auto-promote


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _val(x: float | None) -> float:
    return 0.0 if x is None else float(x)


def _neighbors(c: CandidateEvaluation, others: Sequence[CandidateEvaluation]) -> list[CandidateEvaluation]:
    out = []
    for o in others:
        if o is c or set(o.parameters) != set(c.parameters):
            continue
        if sum(1 for k in c.parameters if c.parameters[k] != o.parameters[k]) == 1:
            out.append(o)
    return out


def _sensitivity(c: CandidateEvaluation, neighbors: Sequence[CandidateEvaluation]) -> float | None:
    if not neighbors:
        return None
    own = _val(c.oos_sharpe)
    drops = [max(0.0, own - _val(n.oos_sharpe)) / (abs(own) + 0.5)
             for n in neighbors]
    return 1.0 - _clip(mean(drops))


def _stability(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    positive = sum(1 for r in returns if r > 0) / len(returns)
    m = mean(returns)
    if m <= 0:
        return 0.0
    cv = pstdev(returns) / m if len(returns) > 1 else 0.0
    return positive / (1.0 + cv)


def rank_candidates(
    candidates: Sequence[CandidateEvaluation],
    config: RankingConfig | None = None,
) -> RankingResult:
    cfg = config or RankingConfig()
    if not candidates:
        raise ValueError("no candidates to rank")
    total_tested = len(candidates)
    scored: list[RankedCandidate] = []
    for c in candidates:
        sens = _sensitivity(c, _neighbors(c, candidates))
        comp: dict[str, float | None] = {
            "oos_performance": _clip(c.oos_total_return / 0.3),
            "drawdown": 1.0 - _clip(c.oos_max_drawdown / cfg.max_oos_drawdown),
            "sharpe": _clip(_val(c.oos_sharpe) / 3.0),
            "sortino": _clip(_val(c.oos_sortino) / 4.0),
            "profit_factor": _clip((_val(c.oos_profit_factor) - 1.0) / 2.0),
            "trade_count": _clip(c.oos_trades / (3 * cfg.min_oos_trades)),
            "stability": _stability(c.fold_returns),
            "sensitivity": sens,
        }
        used = {k: w for k, w in cfg.weights.items() if comp[k] is not None}
        total_w = sum(used.values())
        score = sum(comp[k] * w for k, w in used.items()) / \
            total_w if total_w > 0 else 0.0  # type: ignore[operator]

        reasons = []
        if c.oos_trades < cfg.min_oos_trades:
            reasons.append(
                f"TOO_FEW_OOS_TRADES: {c.oos_trades} < {cfg.min_oos_trades}")
        if c.oos_max_drawdown > cfg.max_oos_drawdown:
            reasons.append(
                f"EXCESSIVE_DRAWDOWN: {c.oos_max_drawdown:.3f} > {cfg.max_oos_drawdown:.3f}")
        if c.oos_total_return <= 0:
            reasons.append("NON_POSITIVE_OOS_RETURN")

        warns = []
        if c.is_sharpe is not None and c.is_sharpe > 0:
            if _val(c.oos_sharpe) * cfg.max_is_oos_sharpe_ratio < c.is_sharpe:
                warns.append("IN_SAMPLE_OUT_OF_SAMPLE_DEGRADATION")
        if _val(c.is_sharpe) > cfg.suspicious_sharpe or _val(c.oos_sharpe) > cfg.suspicious_sharpe \
                or _val(c.oos_profit_factor) > cfg.suspicious_profit_factor:
            warns.append("SUSPICIOUSLY_HIGH_PERFORMANCE")
        if sens is None:
            warns.append("PARAMETER_SENSITIVITY_UNASSESSED")
        elif sens < 0.5:
            warns.append(
                "PARAMETER_SPIKE: neighbouring parameters perform much worse")
        if c.fold_returns and sum(1 for r in c.fold_returns if r > 0) / len(c.fold_returns) < cfg.min_positive_fold_share:
            warns.append("UNSTABLE_ACROSS_FOLDS")
        if total_tested >= cfg.multiple_testing_threshold:
            warns.append(
                f"MULTIPLE_TESTING_RISK: {total_tested} candidates tested")
        scored.append(RankedCandidate(c.parameters, score,
                      comp, tuple(warns), tuple(reasons)))

    ordered = sorted(scored, key=lambda r: (
        bool(r.disqualified), -r.composite_score))
    selected = ordered[0] if not ordered[0].disqualified else None
    top_pnl = max(candidates, key=lambda c: c.total_pnl)
    return RankingResult(
        ranked=tuple(ordered),
        selected=selected,
        highest_pnl_parameters=top_pnl.parameters,
        pnl_pick_differs=selected is not None and dict(
            selected.parameters) != dict(top_pnl.parameters),
        requires_review=selected is None or bool(selected.warnings),
    )


def evaluate_candidates(
    engine: BacktestEngine,
    instruments: Mapping[str, Instrument],
    bars: Mapping[str, pd.DataFrame],
    strategy_factory: Callable[[Mapping[str, Any]], SignalFn],
    parameter_grid: Sequence[Mapping[str, Any]],
    windows: Sequence[WalkForwardWindow],
    **run_kwargs: Any,
) -> list[CandidateEvaluation]:
    """Fixed parameters per candidate: TRAIN gives in-sample figures, VALIDATE the out-of-sample figures; TEST is untouched."""
    out = []
    for params in parameter_grid:
        fn = strategy_factory(params)
        is_runs, oos_runs = [], []
        for w in windows:
            try:
                is_runs.append(engine.run(instruments, bars, fn,
                               start=w.train[0], end=w.train[1], **run_kwargs))
                oos_runs.append(engine.run(
                    instruments, bars, fn, start=w.validate[0], end=w.validate[1], **run_kwargs))
            except ValueError:
                continue
        if not oos_runs:
            continue
        out.append(_aggregate(params, is_runs, oos_runs))
    return out


def confirm_on_test(
    engine: BacktestEngine,
    instruments: Mapping[str, Instrument],
    bars: Mapping[str, pd.DataFrame],
    strategy_factory: Callable[[Mapping[str, Any]], SignalFn],
    selected: RankedCandidate,
    windows: Sequence[WalkForwardWindow],
    **run_kwargs: Any,
) -> list[BacktestResult]:
    """Run the chosen parameters once on the held-out TEST windows; do not re-rank afterwards."""
    fn = strategy_factory(selected.parameters)
    return [engine.run(instruments, bars, fn, start=w.test[0], end=w.test[1], **run_kwargs) for w in windows]


def _aggregate(params: Mapping[str, Any], is_runs: Sequence[BacktestResult],
               oos_runs: Sequence[BacktestResult]) -> CandidateEvaluation:
    def avg(runs: Sequence[BacktestResult], key: str) -> float | None:
        values = [r.metrics[key]
                  for r in runs if r.metrics.get(key) is not None]
        return mean(values) if values else None

    pnls = [t.net_pnl_base for r in oos_runs for t in r.trades]
    wins, losses = sum(p for p in pnls if p > 0), - \
        sum(p for p in pnls if p < 0)
    returns = tuple(float(r.metrics.get("total_return", 0.0))
                    for r in oos_runs)
    total = sum(t.net_pnl_base for r in (*is_runs, *oos_runs)
                for t in r.trades)
    return CandidateEvaluation(
        parameters=dict(params),
        is_sharpe=avg(is_runs, "sharpe"),
        oos_sharpe=avg(oos_runs, "sharpe"),
        oos_sortino=avg(oos_runs, "sortino"),
        oos_profit_factor=wins / losses if losses > 0 else None,
        oos_max_drawdown=max(float(r.metrics.get("max_drawdown", 1.0))
                             for r in oos_runs),
        oos_trades=len(pnls),
        oos_total_return=sum(returns),
        fold_returns=returns,
        total_pnl=total,
    )
