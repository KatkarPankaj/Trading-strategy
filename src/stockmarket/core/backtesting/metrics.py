"""Backtest metrics computed from trades, an equity curve and an exposure series."""

from __future__ import annotations

from math import sqrt
from typing import Any, Sequence

import numpy as np
import pandas as pd

MIN_YEARS_FOR_CAGR = 1 / 12


def compute_metrics(
    trade_pnls: Sequence[float],
    equity: pd.Series,
    exposure: pd.Series,
    traded_notional: float,
    *,
    periods_per_year: int = 252,
    annual_risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    """Undefined statistics are None, never a misleading zero or infinity."""
    if equity.empty or not isinstance(equity.index, pd.DatetimeIndex):
        raise ValueError(
            "equity must be a non-empty Series with a DatetimeIndex")
    if (equity <= 0).any():
        raise ValueError("equity must stay positive for ratio metrics")

    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    years = (equity.index[-1] - equity.index[0]
             ).total_seconds() / (365.25 * 86400)
    cagr = (end / start) ** (1 / years) - \
        1 if years >= MIN_YEARS_FOR_CAGR else None

    daily = equity.groupby(equity.index.date).last()
    returns = daily.pct_change().dropna()
    rf = (1 + annual_risk_free_rate) ** (1 / periods_per_year) - 1
    excess = returns - rf
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    volatility = std * sqrt(periods_per_year) if len(returns) > 1 else None
    sharpe = float(excess.mean()) / std * \
        sqrt(periods_per_year) if std > 0 else None
    downside = sqrt(
        float(np.mean(np.minimum(excess.to_numpy(), 0.0) ** 2))) if len(excess) else 0.0
    sortino = float(excess.mean()) / downside * \
        sqrt(periods_per_year) if downside > 0 else None

    peak = equity.cummax()
    max_dd = float(((peak - equity) / peak).max())
    max_dd_abs = float((peak - equity).max())
    net_profit = end - start

    pnls = np.asarray(trade_pnls, dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "starting_equity": start,
        "ending_equity": end,
        "net_profit": net_profit,
        "total_return": end / start - 1,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": cagr / max_dd if cagr is not None and max_dd > 0 else None,
        "recovery_factor": net_profit / max_dd_abs if max_dd_abs > 0 else None,
        "trades": int(len(pnls)),
        "win_rate": float(len(wins) / len(pnls)) if len(pnls) else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "expectancy": float(pnls.mean()) if len(pnls) else None,
        "average_win": float(wins.mean()) if len(wins) else None,
        "average_loss": float(losses.mean()) if len(losses) else None,
        "exposure": float(exposure.mean()) if len(exposure) else 0.0,
        "turnover": traded_notional / float(equity.mean()),
        "turnover_annualized": traded_notional / float(equity.mean()) / years if years > 0 else None,
        "years": years,
    }
