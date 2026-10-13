"""Deterministic net-return evidence shared by advisory ranking and comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite, sqrt
from statistics import NormalDist, mean, stdev
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class PerformanceObservation:
    identity: str
    opened_at: datetime
    closed_at: datetime
    known_at: datetime
    entry_notional: float
    gross_pnl: float
    fees: float
    net_pnl: float

    def __post_init__(self) -> None:
        if not self.identity or any(t.tzinfo is None or t.utcoffset() is None
                                    for t in (self.opened_at, self.closed_at, self.known_at)):
            raise ValueError("performance observations require identities and aware timestamps")
        if self.closed_at < self.opened_at or self.known_at < self.closed_at:
            raise ValueError("performance observation timestamp order is invalid")
        if any(not isfinite(v) for v in (self.entry_notional, self.gross_pnl, self.fees, self.net_pnl)) \
                or self.entry_notional <= 0 or self.fees < 0 \
                or abs(self.gross_pnl - self.fees - self.net_pnl) > 1e-7:
            raise ValueError("performance observation net PnL or costs are invalid")


def summarize_evidence(
    observations: Sequence[PerformanceObservation], *, as_of: datetime,
    comparisons: int = 1,
) -> dict[str, Any]:
    """Three chronological blocks; Bonferroni-normal bound, not a calibrated forecast."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("evidence as_of must be timezone-aware")
    if isinstance(comparisons, bool) or not isinstance(comparisons, int) or comparisons < 1:
        raise ValueError("comparisons must be a positive integer")
    ordered = sorted(observations, key=lambda o: (o.closed_at, o.identity))
    if len({o.identity for o in ordered}) != len(ordered):
        raise ValueError("duplicate observations would double-count performance")
    if any(o.closed_at > as_of or o.known_at > as_of for o in ordered):
        raise ValueError("future outcomes cannot inform earlier evidence")
    # Concurrent episodes do not constitute independent samples for this v1 estimator.
    running_close = None
    for observation in sorted(ordered, key=lambda o: (o.opened_at, o.closed_at)):
        if running_close is not None and observation.opened_at < running_close:
            raise ValueError("overlapping episodes require a portfolio-level uncertainty estimator")
        running_close = observation.closed_at
    count = len(ordered)
    returns = [o.net_pnl / o.entry_notional for o in ordered]
    pnls = [o.net_pnl for o in ordered]
    wins = sum(v for v in pnls if v > 0)
    losses = -sum(v for v in pnls if v < 0)
    cumulative = peak = drawdown = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    blocks = []
    for n in range(3):
        block = ordered[n * count // 3:(n + 1) * count // 3]
        if block:
            values = [o.net_pnl / o.entry_notional for o in block]
            blocks.append({
                "start": block[0].closed_at.isoformat(), "end": block[-1].closed_at.isoformat(),
                "trade_count": len(block), "net_return_expectancy": mean(values),
                "net_pnl": sum(o.net_pnl for o in block),
            })
    # Calendar/session spread matters: thirty same-day wins are not thirty independent days.
    days = len({o.closed_at.date() for o in ordered})
    qualified = count >= 30 and days >= 10 and len(blocks) == 3
    average = mean(returns) if returns else None
    z = NormalDist().inv_cdf(1 - .05 / (2 * comparisons))
    uncertainty = z * stdev(returns) / sqrt(count) if count > 1 else None
    instability = stdev([b["net_return_expectancy"] for b in blocks]) if len(blocks) > 1 else None
    downside = max(0.0, -(min(b["net_return_expectancy"] for b in blocks))) if blocks else 0.0
    drawdown_penalty = drawdown / count if count else 0.0
    score = average - uncertainty - (instability or 0) - downside - drawdown_penalty \
        if qualified and average is not None and uncertainty is not None else None
    return {
        "sample_status": "QUALIFIED_COMPARATIVE_SAMPLE" if qualified else "INSUFFICIENT_EVIDENCE",
        "trade_count": count, "observed_days": days,
        "win_rate": sum(v > 0 for v in pnls) / count if count else None,
        "net_expectancy": mean(pnls) if pnls else None, "net_return_expectancy": average,
        "profit_factor": wins / losses if losses else None,
        "profit_factor_status": "DEFINED" if losses else "NO_LOSSES" if count else "NO_TRADES",
        "gross_pnl": sum(o.gross_pnl for o in ordered), "fees": sum(o.fees for o in ordered),
        "net_pnl": sum(pnls), "closed_return_drawdown": drawdown,
        "chronological_windows": blocks, "window_instability": instability,
        "worst_window_penalty": downside, "drawdown_penalty": drawdown_penalty,
        "uncertainty_half_width": uncertainty, "comparisons": comparisons,
        "confidence_z": z, "score": score,
        "performance_status": "INSUFFICIENT_EVIDENCE" if not qualified else
            "BELOW_NO_TRADE" if score <= 0 else "ABOVE_NO_TRADE_UNDER_POLICY",
        "limitations": [
            "Normal approximation and chronological blocks do not establish independent market samples.",
            "Closed-return drawdown excludes unrealized portfolio drawdown.",
            "Multiplicity covers this catalog only, not undisclosed previous experiments.",
        ],
    }
