from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stockmarket.config import TradingConfig
from stockmarket.strategy import add_strategy_columns


def _backtest_use_cycle() -> bool:
    return os.environ.get("BACKTEST_USE_CYCLE") == "1"


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    summary: dict[str, float]


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


def run_backtest(df: pd.DataFrame, cfg: TradingConfig) -> BacktestResult:
    if _backtest_use_cycle():
        from .cycle import run_backtest_via_cycle

        trades_df, summary = run_backtest_via_cycle(df, cfg)
        return BacktestResult(trades=trades_df, summary=summary)

    return _run_backtest_legacy(df, cfg)


def _run_backtest_legacy(df: pd.DataFrame, cfg: TradingConfig) -> BacktestResult:
    data = add_strategy_columns(df, cfg)

    square_off_t = time.fromisoformat(cfg.square_off_time)
    initial_capital = cfg.starting_capital
    capital = initial_capital

    trades: list[dict[str, Any]] = []
    equity_points: list[float] = [capital]

    for date_key, day_df in data.groupby(data.index.date, sort=True):
        position: dict[str, Any] | None = None
        trades_today = 0

        for ts, row in day_df.iterrows():
            if position is None and trades_today < cfg.max_trades_per_day:
                go_long = bool(row["long_signal"])
                go_short = bool(row["short_signal"])

                if go_long or go_short:
                    side = "long" if go_long else "short"
                    raw_entry = float(row["close"])
                    entry = raw_entry * (
                        1 + cfg.slippage_pct if side == "long" else 1 - cfg.slippage_pct
                    )
                    qty = _position_size(entry, capital, cfg)

                    if qty > 0:
                        position = {
                            "side": side,
                            "entry_ts": ts,
                            "entry_price": entry,
                            "qty": qty,
                            "stop_price": entry
                            * (1 - cfg.stop_loss_pct if side == "long" else 1 + cfg.stop_loss_pct),
                            "target_price": entry
                            * (1 + cfg.take_profit_pct if side == "long" else 1 - cfg.take_profit_pct),
                            "time_exit_ts": ts
                            + timedelta(minutes=cfg.time_exit_minutes),
                        }
                        trades_today += 1

            if position is None:
                continue

            side = position["side"]
            qty = int(position["qty"])
            stop = float(position["stop_price"])
            target = float(position["target_price"])

            bar_low = float(row["low"])
            bar_high = float(row["high"])
            raw_close = float(row["close"])

            exit_reason = None
            exit_price = None

            if side == "long":
                if bar_low <= stop:
                    exit_reason = "stop"
                    exit_price = stop * (1 - cfg.slippage_pct)
                elif bar_high >= target:
                    exit_reason = "target"
                    exit_price = target * (1 - cfg.slippage_pct)
            else:
                if bar_high >= stop:
                    exit_reason = "stop"
                    exit_price = stop * (1 + cfg.slippage_pct)
                elif bar_low <= target:
                    exit_reason = "target"
                    exit_price = target * (1 + cfg.slippage_pct)

            if exit_reason is None and ts >= position["time_exit_ts"]:
                exit_reason = "time"
                exit_price = raw_close * (
                    1 - cfg.slippage_pct if side == "long" else 1 + cfg.slippage_pct
                )

            if exit_reason is None and ts.time() >= square_off_t:
                exit_reason = "square_off"
                exit_price = raw_close * (
                    1 - cfg.slippage_pct if side == "long" else 1 + cfg.slippage_pct
                )

            if exit_reason is None:
                continue

            entry_price = float(position["entry_price"])
            turnover = (entry_price * qty) + (exit_price * qty)
            commission = turnover * cfg.commission_pct

            gross_pnl = (
                (exit_price - entry_price) * qty
                if side == "long"
                else (entry_price - exit_price) * qty
            )
            net_pnl = gross_pnl - commission
            capital += net_pnl

            trades.append(
                {
                    "date": str(date_key),
                    "side": side,
                    "entry_ts": position["entry_ts"],
                    "exit_ts": ts,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": qty,
                    "gross_pnl": gross_pnl,
                    "commission": commission,
                    "net_pnl": net_pnl,
                    "exit_reason": exit_reason,
                    "capital_after_trade": capital,
                }
            )
            equity_points.append(capital)
            position = None

        if position is not None:
            last_ts = day_df.index[-1]
            last_close = float(day_df.iloc[-1]["close"])
            side = position["side"]
            qty = int(position["qty"])
            exit_price = last_close * (
                1 - cfg.slippage_pct if side == "long" else 1 + cfg.slippage_pct
            )
            entry_price = float(position["entry_price"])

            turnover = (entry_price * qty) + (exit_price * qty)
            commission = turnover * cfg.commission_pct
            gross_pnl = (
                (exit_price - entry_price) * qty
                if side == "long"
                else (entry_price - exit_price) * qty
            )
            net_pnl = gross_pnl - commission
            capital += net_pnl

            trades.append(
                {
                    "date": str(date_key),
                    "side": side,
                    "entry_ts": position["entry_ts"],
                    "exit_ts": last_ts,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": qty,
                    "gross_pnl": gross_pnl,
                    "commission": commission,
                    "net_pnl": net_pnl,
                    "exit_reason": "forced_day_end",
                    "capital_after_trade": capital,
                }
            )
            equity_points.append(capital)

    trades_df = pd.DataFrame(trades)

    if trades_df.empty:
        summary = {
            "total_trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
        }
        return BacktestResult(trades=trades_df, summary=summary)

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] < 0]

    gross_profit = float(wins["net_pnl"].sum())
    gross_loss = float(abs(losses["net_pnl"].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    equity = pd.Series(equity_points)
    summary = {
        "total_trades": float(len(trades_df)),
        "win_rate": float((trades_df["net_pnl"] > 0).mean()),
        "net_pnl": float(trades_df["net_pnl"].sum()),
        "return_pct": float((capital - initial_capital) / initial_capital),
        "max_drawdown_pct": float(_compute_max_drawdown(equity)),
        "profit_factor": float(profit_factor),
    }

    return BacktestResult(trades=trades_df, summary=summary)
