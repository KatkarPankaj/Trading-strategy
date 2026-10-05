"""Deterministic performance statistics for backtest trades and equity curves."""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any

import pandas as pd


def calculate_performance_metrics(
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
    *,
    starting_capital: float,
    periods_per_year: int = 252,
    annual_risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    """Calculate trade metrics and daily-equity Sharpe/Sortino statistics.

    ``equity_curve`` must contain ``timestamp``, ``equity`` and ``position_open``.
    Sharpe/Sortino use daily closing-equity returns, not individual trade returns.
    Undefined metrics are ``None`` and have a companion ``*_status`` field.
    """
    _positive_finite(starting_capital, "starting_capital")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int):
        raise TypeError("periods_per_year must be an integer")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    _finite(annual_risk_free_rate, "annual_risk_free_rate")
    if annual_risk_free_rate <= -1:
        raise ValueError("annual_risk_free_rate must be greater than -1")
    trades = _validate_trades(trades)
    curve = _validate_equity_curve(equity_curve)

    pnl = pd.to_numeric(trades.get(
        "net_pnl", pd.Series(dtype=float)), errors="coerce")
    if not pnl.empty and (pnl.isna().any() or not pnl.map(isfinite).all()):
        raise ValueError("net_pnl values must be finite")
    pnl_values = [float(value) for value in pnl.tolist()]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
        profit_factor_status = "defined"
    elif gross_profit > 0:
        profit_factor = None
        profit_factor_status = "undefined_no_losses"
    else:
        profit_factor = None
        profit_factor_status = "undefined_no_winning_or_losing_trades"

    net_pnl = sum(pnl_values)
    total_trades = len(pnl_values)
    if total_trades:
        expectancy = net_pnl / total_trades
        win_rate = len(wins) / total_trades
        average_win = sum(wins) / len(wins) if wins else None
        average_loss = sum(losses) / len(losses) if losses else None
    else:
        expectancy = None
        win_rate = None
        average_win = None
        average_loss = None

    ending_equity = float(curve["equity"].iloc[-1]
                          ) if not curve.empty else starting_capital
    total_return = ending_equity / starting_capital - 1.0
    max_drawdown = _max_drawdown(curve["equity"]) if not curve.empty else 0.0
    exposure = (
        float(curve["position_open"].astype(bool).mean())
        if not curve.empty
        else 0.0
    )
    max_consecutive_wins, max_consecutive_losses = _consecutive_streaks(
        pnl_values)
    sharpe, sharpe_status, sortino, sortino_status = _daily_risk_ratios(
        curve,
        starting_capital=starting_capital,
        periods_per_year=periods_per_year,
        annual_risk_free_rate=annual_risk_free_rate,
    )

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "total_return": total_return,
        "return_pct": total_return,
        "profit_factor": profit_factor,
        "profit_factor_status": profit_factor_status,
        "expectancy": expectancy,
        "maximum_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown,
        "sharpe_ratio": sharpe,
        "sharpe_status": sharpe_status,
        "sortino_ratio": sortino,
        "sortino_status": sortino_status,
        "average_win": average_win,
        "average_loss": average_loss,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "exposure": exposure,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "periods_per_year": periods_per_year,
        "annual_risk_free_rate": annual_risk_free_rate,
        "risk_ratio_basis": "daily_closing_equity_returns",
        "exposure_basis": "fraction_of_observed_bars_with_position_open",
    }


def _validate_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(trades, pd.DataFrame):
        raise TypeError("trades must be a pandas DataFrame")
    if trades.empty:
        return trades.copy()
    required = {"net_pnl"}
    missing = required.difference(trades.columns)
    if missing:
        raise ValueError(f"trades missing required columns: {sorted(missing)}")
    return trades.copy()


def _validate_equity_curve(curve: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(curve, pd.DataFrame):
        raise TypeError("equity_curve must be a pandas DataFrame")
    if curve.empty:
        return curve.copy()
    required = {"timestamp", "equity", "position_open"}
    missing = required.difference(curve.columns)
    if missing:
        raise ValueError(
            f"equity_curve missing required columns: {sorted(missing)}")
    result = curve.copy()
    timestamps = pd.to_datetime(result["timestamp"], errors="raise")
    if timestamps.dt.tz is None:
        raise ValueError("equity-curve timestamps must be timezone-aware")
    if not timestamps.is_monotonic_increasing or timestamps.duplicated().any():
        raise ValueError("equity-curve timestamps must be sorted and unique")
    result["timestamp"] = timestamps
    values = pd.to_numeric(result["equity"], errors="coerce")
    if values.isna().any() or not values.map(isfinite).all() or (values <= 0).any():
        raise ValueError("equity values must be finite and positive")
    if not result["position_open"].map(lambda value: isinstance(value, bool)).all():
        raise ValueError("position_open values must be booleans")
    result["equity"] = values.astype(float)
    return result


def _max_drawdown(equity: pd.Series) -> float:
    values = equity.astype(float)
    peaks = values.cummax()
    return float(((values - peaks) / peaks).min())


def _consecutive_streaks(pnl: list[float]) -> tuple[int, int]:
    max_wins = max_losses = current_wins = current_losses = 0
    for value in pnl:
        if value > 0:
            current_wins += 1
            current_losses = 0
        elif value < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        max_wins = max(max_wins, current_wins)
        max_losses = max(max_losses, current_losses)
    return max_wins, max_losses


def _daily_risk_ratios(
    curve: pd.DataFrame,
    *,
    starting_capital: float,
    periods_per_year: int,
    annual_risk_free_rate: float,
) -> tuple[float | None, str, float | None, str]:
    if curve.empty:
        return None, "undefined_no_equity_observations", None, "undefined_no_equity_observations"

    daily_equity = curve.set_index(
        "timestamp")["equity"].resample("1D").last().dropna()
    if daily_equity.empty:
        return None, "undefined_no_daily_equity", None, "undefined_no_daily_equity"
    prior_equity = daily_equity.shift(1)
    prior_equity.iloc[0] = starting_capital
    returns = daily_equity.div(prior_equity).sub(1.0)
    returns = returns.replace([float("inf"), float("-inf")], pd.NA).dropna()
    if len(returns) < 2:
        return None, "undefined_insufficient_daily_returns", None, "undefined_insufficient_daily_returns"

    daily_rf = (1.0 + annual_risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    excess = returns - daily_rf
    deviation = float(excess.std(ddof=1))
    sharpe = (
        float(excess.mean() / deviation * sqrt(periods_per_year))
        if deviation > 0
        else None
    )
    sharpe_status = "defined" if sharpe is not None else "undefined_zero_volatility"

    downside = excess.clip(upper=0.0)
    downside_deviation = float((downside.pow(2).mean()) ** 0.5)
    sortino = (
        float(excess.mean() / downside_deviation * sqrt(periods_per_year))
        if downside_deviation > 0
        else None
    )
    sortino_status = "defined" if sortino is not None else "undefined_no_downside_returns"
    return sharpe, sharpe_status, sortino, sortino_status


def _finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _positive_finite(value: float, field_name: str) -> None:
    _finite(value, field_name)
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
