from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any

import numpy as np
import pandas as pd

from .config import TradingConfig
from .core.market_session import MarketSession
from .strategy import add_strategy_columns
from .validation.statistics import calculate_performance_metrics


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    summary: dict[str, Any]
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    execution_policy: str = "next_bar_open_stop_first"


def _compute_max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max.replace(0, np.nan)
    return float(drawdown.min())


def _position_size(entry_price: float, capital: float, cfg: TradingConfig) -> int:
    per_share_risk = entry_price * cfg.stop_loss_pct
    if per_share_risk <= 0:
        return 0
    risk_budget = capital * cfg.risk_per_trade_pct
    qty = int(risk_budget // per_share_risk)
    return max(0, qty)


def validate_ohlcv_data(df: pd.DataFrame) -> pd.DataFrame:
    """Validate and copy OHLCV bars before any indicators or trades are computed."""
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    if df.empty:
        raise ValueError("OHLCV data must not be empty")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("OHLCV index must be a DatetimeIndex")
    if df.index.tz is None:
        raise ValueError("OHLCV timestamps must be timezone-aware")
    if df.index.hasnans:
        raise ValueError("OHLCV timestamps must not contain NaT")
    if not df.index.is_monotonic_increasing:
        raise ValueError("OHLCV timestamps must be sorted in ascending order")
    if df.index.has_duplicates:
        raise ValueError("OHLCV timestamps must be unique")

    required = ("open", "high", "low", "close", "volume")
    missing = set(required).difference(df.columns)
    if missing:
        raise ValueError(
            f"OHLCV data missing required columns: {sorted(missing)}")
    data = df.copy()
    for column in required:
        try:
            data[column] = pd.to_numeric(
                data[column], errors="raise").astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"OHLCV column {column!r} must be numeric") from exc
        values = data[column]
        if values.isna().any() or not values.map(isfinite).all():
            raise ValueError(
                f"OHLCV column {column!r} must contain finite values")

    prices = data[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        raise ValueError("OHLC prices must be greater than zero")
    if (data["volume"] < 0).any():
        raise ValueError("volume must be non-negative")
    if (data["high"] < prices[["open", "low", "close"]].max(axis=1)).any():
        raise ValueError("high must be >= open, low, and close")
    if (data["low"] > prices[["open", "high", "close"]].min(axis=1)).any():
        raise ValueError("low must be <= open, high, and close")
    return data


def _adverse_price(price: float, side: str, *, entry: bool, slippage_pct: float) -> float:
    adverse_up = (side == "long") == entry
    return price * (1 + slippage_pct if adverse_up else 1 - slippage_pct)


def _bar_exit(
    position: dict[str, Any],
    timestamp: pd.Timestamp,
    row: pd.Series,
    market_session: MarketSession,
) -> tuple[str | None, float | None]:
    side = str(position["side"])
    stop = float(position["stop_price"])
    target = float(position["target_price"])
    opening = float(row["open"])
    low = float(row["low"])
    high = float(row["high"])
    if side == "long":
        if low <= stop:
            return "stop", opening if opening <= stop else stop
        if high >= target:
            return "target", opening if opening >= target else target
    else:
        if high >= stop:
            return "stop", opening if opening >= stop else stop
        if low <= target:
            return "target", opening if opening <= target else target
    if timestamp >= position["time_exit_ts"]:
        return "time", float(row["close"])
    if market_session.is_square_off(timestamp.to_pydatetime()):
        return "square_off", float(row["close"])
    return None, None


def _closed_trade(
    position: dict[str, Any],
    *,
    exit_ts: pd.Timestamp,
    exit_price: float,
    exit_reason: str,
    capital: float,
    commission_pct: float,
) -> dict[str, Any]:
    side = str(position["side"])
    entry_price = float(position["entry_price"])
    quantity = int(position["qty"])
    turnover = (entry_price + exit_price) * quantity
    commission = turnover * commission_pct
    gross_pnl = (
        (exit_price - entry_price) * quantity
        if side == "long"
        else (entry_price - exit_price) * quantity
    )
    net_pnl = gross_pnl - commission
    return {
        "date": str(exit_ts.date()),
        "side": side,
        "signal_ts": position["signal_ts"],
        "entry_ts": position["entry_ts"],
        "exit_ts": exit_ts,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "qty": quantity,
        "gross_pnl": gross_pnl,
        "commission": commission,
        "net_pnl": net_pnl,
        "exit_reason": exit_reason,
        "capital_after_trade": capital + net_pnl,
    }


def _validate_run_options(
    cfg: TradingConfig,
    periods_per_year: int,
    annual_risk_free_rate: float,
    commission_multiplier: float,
    slippage_multiplier: float,
) -> None:
    if not isinstance(cfg, TradingConfig):
        raise TypeError("cfg must be a TradingConfig")
    for name in (
        "starting_capital", "risk_per_trade_pct", "stop_loss_pct",
        "take_profit_pct", "commission_pct", "slippage_pct",
    ):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"config {name} must be numeric")
        if not isfinite(value) or value < 0:
            raise ValueError(f"config {name} must be finite and non-negative")
    if cfg.starting_capital <= 0:
        raise ValueError("starting_capital must be positive")
    if cfg.stop_loss_pct <= 0 or cfg.take_profit_pct <= 0:
        raise ValueError("stop_loss_pct and take_profit_pct must be positive")
    for name in ("max_trades_per_day", "time_exit_minutes", "volume_ma_window"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"config {name} must be a positive integer")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int):
        raise TypeError("periods_per_year must be an integer")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if annual_risk_free_rate <= -1:
        raise ValueError("annual_risk_free_rate must be greater than -1")
    for name, value in (
        ("annual_risk_free_rate", annual_risk_free_rate),
        ("commission_multiplier", commission_multiplier),
        ("slippage_multiplier", slippage_multiplier),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
        if not isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")


def run_backtest(
    df: pd.DataFrame,
    cfg: TradingConfig,
    *,
    periods_per_year: int = 252,
    annual_risk_free_rate: float = 0.0,
    commission_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
    evaluation_start: date | None = None,
    evaluation_end: date | None = None,
) -> BacktestResult:
    """Run a causal next-observed-bar-open backtest with mark-to-market equity."""
    data = validate_ohlcv_data(df)
    _validate_run_options(
        cfg,
        periods_per_year,
        annual_risk_free_rate,
        commission_multiplier,
        slippage_multiplier,
    )
    session = MarketSession.from_config(cfg)
    local_index = data.index.tz_convert(session.zone)
    in_session = [
        session.market_open <= local_timestamp.time().replace(tzinfo=None)
        <= session.market_close
        for local_timestamp in local_index
    ]
    data = data.loc[in_session]
    if data.empty:
        raise ValueError(
            "OHLCV data has no observations inside the configured market session"
        )
    data = data.copy()
    data.index = data.index.tz_convert(session.zone)
    data = add_strategy_columns(data, cfg)
    if evaluation_start is not None and not isinstance(evaluation_start, date):
        raise TypeError("evaluation_start must be a date")
    if evaluation_end is not None and not isinstance(evaluation_end, date):
        raise TypeError("evaluation_end must be a date")
    if evaluation_start is not None and evaluation_end is not None:
        if evaluation_end < evaluation_start:
            raise ValueError(
                "evaluation_end must not precede evaluation_start")
    if evaluation_start is not None:
        data = data.loc[data.index.date >= evaluation_start]
    if evaluation_end is not None:
        data = data.loc[data.index.date <= evaluation_end]
    if data.empty:
        raise ValueError(
            "No OHLCV bars fall inside the requested evaluation dates")
    initial_capital = float(cfg.starting_capital)
    capital = initial_capital
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    effective_slippage = cfg.slippage_pct * slippage_multiplier
    effective_commission = cfg.commission_pct * commission_multiplier

    for _, day_df in data.groupby(data.index.date, sort=True):
        position: dict[str, Any] | None = None
        pending_signal: dict[str, Any] | None = None
        trades_today = 0

        for timestamp, row in day_df.iterrows():
            flat_at_start = position is None and pending_signal is None

            if position is None and pending_signal is not None:
                side = str(pending_signal["side"])
                entry_price = _adverse_price(
                    float(row["open"]), side, entry=True,
                    slippage_pct=effective_slippage,
                )
                quantity = _position_size(entry_price, capital, cfg)
                signal_timestamp = pending_signal["signal_ts"]
                pending_signal = None
                if quantity > 0:
                    position = {
                        "side": side,
                        "signal_ts": signal_timestamp,
                        "entry_ts": timestamp,
                        "entry_price": entry_price,
                        "qty": quantity,
                        "stop_price": entry_price * (
                            1 - cfg.stop_loss_pct if side == "long"
                            else 1 + cfg.stop_loss_pct
                        ),
                        "target_price": entry_price * (
                            1 + cfg.take_profit_pct if side == "long"
                            else 1 - cfg.take_profit_pct
                        ),
                        "time_exit_ts": timestamp + timedelta(minutes=cfg.time_exit_minutes),
                    }
                    trades_today += 1

            if position is not None:
                exit_reason, raw_exit = _bar_exit(
                    position, timestamp, row, session)
                if exit_reason is not None and raw_exit is not None:
                    exit_price = _adverse_price(
                        raw_exit,
                        str(position["side"]),
                        entry=False,
                        slippage_pct=effective_slippage,
                    )
                    trade = _closed_trade(
                        position,
                        exit_ts=timestamp,
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        capital=capital,
                        commission_pct=effective_commission,
                    )
                    capital = float(trade["capital_after_trade"])
                    trades.append(trade)
                    position = None

            if (
                flat_at_start
                and position is None
                and pending_signal is None
                and trades_today < cfg.max_trades_per_day
            ):
                go_long = bool(row["long_signal"])
                go_short = bool(row["short_signal"])
                if go_long or go_short:
                    pending_signal = {
                        "side": "long" if go_long else "short",
                        "signal_ts": timestamp,
                    }

            unrealized = 0.0
            if position is not None:
                marked_price = float(row["close"])
                entry_price = float(position["entry_price"])
                quantity = int(position["qty"])
                estimated_exit = _adverse_price(
                    marked_price,
                    str(position["side"]),
                    entry=False,
                    slippage_pct=effective_slippage,
                )
                unrealized = (
                    (estimated_exit - entry_price) * quantity
                    if position["side"] == "long"
                    else (entry_price - estimated_exit) * quantity
                )
                estimated_round_trip_commission = (
                    entry_price + estimated_exit
                ) * quantity * effective_commission
                unrealized -= estimated_round_trip_commission
            equity_rows.append(
                {
                    "timestamp": timestamp,
                    "equity": capital + unrealized,
                    "realized_capital": capital,
                    "position_open": position is not None,
                }
            )

        if position is not None:
            last_timestamp = day_df.index[-1]
            exit_price = _adverse_price(
                float(day_df.iloc[-1]["close"]),
                str(position["side"]),
                entry=False,
                slippage_pct=effective_slippage,
            )
            trade = _closed_trade(
                position,
                exit_ts=last_timestamp,
                exit_price=exit_price,
                exit_reason="forced_day_end",
                capital=capital,
                commission_pct=effective_commission,
            )
            capital = float(trade["capital_after_trade"])
            trades.append(trade)
            if equity_rows and equity_rows[-1]["timestamp"] == last_timestamp:
                equity_rows[-1].update(
                    equity=capital,
                    realized_capital=capital,
                    position_open=False,
                )

    trades_df = pd.DataFrame(trades)
    equity_curve = pd.DataFrame(equity_rows)
    summary = calculate_performance_metrics(
        trades_df,
        equity_curve,
        starting_capital=initial_capital,
        periods_per_year=periods_per_year,
        annual_risk_free_rate=annual_risk_free_rate,
    )
    # Preserve numeric values expected by the existing webapp and sweep views.
    if summary["win_rate"] is None:
        summary["win_rate"] = 0.0
    if summary["profit_factor"] is None:
        summary["profit_factor"] = (
            float("inf") if summary["gross_profit"] > 0 else 0.0
        )
    return BacktestResult(
        trades=trades_df,
        summary=summary,
        equity_curve=equity_curve,
    )
