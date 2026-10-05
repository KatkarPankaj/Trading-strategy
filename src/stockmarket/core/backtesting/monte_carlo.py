"""Monte Carlo analysis of trade sequencing: how bad could the path have been with the same trades."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    method: str
    simulations: int
    seed: int
    trades: int
    final_equity: dict[str, float]
    max_drawdown: dict[str, float]
    probability_of_loss: float
    probability_of_ruin: float
    ruin_drawdown: float


def monte_carlo_trade_analysis(
    trade_pnls: Sequence[float],
    starting_equity: float,
    *,
    seed: int,
    simulations: int = 2000,
    method: Literal["shuffle", "bootstrap"] = "shuffle",
    ruin_drawdown: float = 0.5,
) -> MonteCarloResult:
    """Shuffle keeps the final equity fixed and varies the path; bootstrap also varies the outcome."""
    pnls = np.asarray(trade_pnls, dtype=float)
    if len(pnls) < 2 or not np.isfinite(pnls).all():
        raise ValueError("need at least two finite trade results")
    if not starting_equity > 0:
        raise ValueError("starting_equity must be positive")
    if method not in ("shuffle", "bootstrap"):
        raise ValueError("method must be 'shuffle' or 'bootstrap'")
    if simulations < 100 or not 0 < ruin_drawdown <= 1:
        raise ValueError(
            "simulations must be >= 100 and ruin_drawdown in (0, 1]")

    rng = np.random.default_rng(seed)
    if method == "shuffle":
        sims = rng.permuted(np.tile(pnls, (simulations, 1)), axis=1)
    else:
        sims = rng.choice(pnls, size=(simulations, len(pnls)), replace=True)

    equity = starting_equity + np.cumsum(sims, axis=1)
    peak = np.maximum(np.maximum.accumulate(equity, axis=1), starting_equity)
    drawdown = np.clip((peak - equity) / peak, 0.0, 1.0).max(axis=1)
    final = equity[:, -1]

    def pct(values: np.ndarray, points: tuple[int, ...]) -> dict[str, float]:
        return {f"p{p}": float(np.percentile(values, p)) for p in points}

    return MonteCarloResult(
        method=method, simulations=simulations, seed=seed, trades=len(pnls),
        final_equity=pct(final, (5, 50, 95)),
        max_drawdown=pct(drawdown, (50, 95, 99)),
        probability_of_loss=float((final < starting_equity).mean()),
        probability_of_ruin=float((drawdown >= ruin_drawdown).mean()),
        ruin_drawdown=ruin_drawdown,
    )
