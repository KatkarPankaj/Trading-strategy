"""Backtest via shared run_cycle pipeline."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from stockmarket.config import TradingConfig
from stockmarket.cycle.adapters.log_history import LogHistoryQuery
from stockmarket.cycle.adapters.session_prices import NoOpPriceRefresh
from stockmarket.cycle.context import CycleContext
from stockmarket.cycle.entry.cooldown import DefaultCooldownPolicy
from stockmarket.cycle.entry.idle_fallback import DefaultIdleFallbackPolicy
from stockmarket.cycle.runner import run_cycle
from stockmarket.cycle.services import Services
from stockmarket.domain.types import DailyCounters, PaperState
from stockmarket.strategy import add_strategy_columns

from .adapters import HistoricalBroker, HistoricalClock, HistoricalSignalSource
from .settings import cycle_settings_from_trading_config
from .sizer import BacktestPositionSizer
from .steps import (
    backtest_bar_exits,
    backtest_force_day_end,
    backtest_place_long_entries,
    backtest_place_short_entries,
    backtest_refresh_bar,
)


class _MemoryRepo:
    def load(self):
        return None

    def save(self, state, counters):
        self.state = state
        self.counters = counters

    def path(self):
        from pathlib import Path

        return Path("memory")


BACKTEST_STEPS = (
    backtest_refresh_bar,
    backtest_place_long_entries,
    backtest_place_short_entries,
    backtest_bar_exits,
)


def _commission(side: str, turnover: float, cfg: TradingConfig) -> float:
    return turnover * float(cfg.commission_pct)


def _trades_from_log(
    log: list, *, initial_capital: float, cfg: TradingConfig
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    capital = float(initial_capital)
    pending: dict[str, dict[str, Any]] = {}

    for row in log:
        sym = str(row.symbol)
        side_u = str(row.side).upper()
        if side_u in {"BUY", "SHORT"}:
            pending[sym] = {
                "side": "long" if side_u == "BUY" else "short",
                "entry_ts": row.ts,
                "entry_price": float(row.price),
                "qty": int(row.qty),
            }
            continue
        if side_u not in {"SELL", "COVER"} or sym not in pending:
            continue

        entry = pending.pop(sym)
        exit_price = float(row.price)
        qty = int(entry["qty"])
        entry_price = float(entry["entry_price"])
        side = str(entry["side"])
        turnover = (entry_price * qty) + (exit_price * qty)
        commission = _commission(side_u, turnover, cfg)
        gross_pnl = (
            (exit_price - entry_price) * qty
            if side == "long"
            else (entry_price - exit_price) * qty
        )
        net_pnl = gross_pnl - commission
        capital += net_pnl
        reason = str(row.reason).lower()
        if "stop" in reason:
            exit_reason = "stop"
        elif "target" in reason or "tp" in reason:
            exit_reason = "target"
        elif "time" in reason:
            exit_reason = "time"
        elif "square" in reason:
            exit_reason = "square_off"
        elif "forced" in reason or "day_end" in reason:
            exit_reason = "forced_day_end"
        else:
            exit_reason = reason or "unknown"

        trades.append(
            {
                "date": str(pd.Timestamp(entry["entry_ts"]).date()),
                "side": side,
                "entry_ts": entry["entry_ts"],
                "exit_ts": row.ts,
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
    return trades


def run_backtest_via_cycle(df: pd.DataFrame, cfg: TradingConfig) -> tuple[pd.DataFrame, dict[str, float]]:
    from . import _compute_max_drawdown

    data = add_strategy_columns(df, cfg)
    symbol = str(cfg.symbol)
    settings = cycle_settings_from_trading_config(cfg, [symbol])
    initial_capital = float(cfg.starting_capital)

    state = PaperState(
        start_capital=initial_capital,
        cash=initial_capital,
        realized=0.0,
        charges=0.0,
        holdings={},
        shorts={},
        prices={},
        log=[],
    )
    counters = DailyCounters(day="")
    clock = HistoricalClock(cfg)
    broker = HistoricalBroker(cfg, lambda side, val: _commission(side, val, cfg))
    signals = HistoricalSignalSource(symbol, data.iloc[0] if len(data) else pd.Series())
    svc = Services(
        clock=clock,
        broker=broker,
        signals=signals,
        history=LogHistoryQuery(clock.market_open_time()),
        repo=_MemoryRepo(),
        prices=NoOpPriceRefresh(),
        charges_fn=lambda side, val: _commission(side, val, cfg),
        cooldown=DefaultCooldownPolicy(),
        sizer=BacktestPositionSizer(cfg),
        idle_fallback=DefaultIdleFallbackPolicy(),
    )

    running_capital = initial_capital
    for date_key, day_df in data.groupby(data.index.date, sort=True):
        day_str = str(date_key)
        counters.day = day_str
        broker._open.clear()
        ctx_entries_today = 0

        for ts, row in day_df.iterrows():
            now = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            clock.set_now(now)
            broker.set_bar(row)
            signals.set_bar(row)
            state.ui_config["_risk_capital"] = running_capital

            ctx = CycleContext(
                now=clock.now(),
                today=day_str,
                settings=settings,
                state=state,
                counters=counters,
                signals=signals.rank(state, settings),
                entries_today=ctx_entries_today,
                open_positions=len(state.holdings) + len(state.shorts),
            )
            log_len_before = len(state.log)
            ctx = run_cycle(ctx, svc, steps=BACKTEST_STEPS)
            ctx_entries_today = svc.history.today_entry_count(state, day_str)
            if len(state.log) > log_len_before:
                closed = _trades_from_log(
                    state.log, initial_capital=initial_capital, cfg=cfg
                )
                if closed:
                    running_capital = float(closed[-1]["capital_after_trade"])

        if symbol in broker.open_positions:
            last_ts = day_df.index[-1]
            last_row = day_df.iloc[-1]
            now = (
                last_ts.to_pydatetime()
                if hasattr(last_ts, "to_pydatetime")
                else last_ts
            )
            clock.set_now(now)
            broker.set_bar(last_row)
            ctx = CycleContext(
                now=clock.now(),
                today=day_str,
                settings=settings,
                state=state,
                counters=counters,
                signals=signals.rank(state, settings),
                entries_today=ctx_entries_today,
                open_positions=len(state.holdings) + len(state.shorts),
            )
            backtest_force_day_end(ctx, svc)

    trades = _trades_from_log(state.log, initial_capital=initial_capital, cfg=cfg)
    if trades:
        state.ui_config["_risk_capital"] = float(trades[-1]["capital_after_trade"])
    trades_df = pd.DataFrame(trades)

    if trades_df.empty:
        return trades_df, {
            "total_trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
        }

    capital = float(trades_df["capital_after_trade"].iloc[-1])
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] < 0]
    gross_profit = float(wins["net_pnl"].sum())
    gross_loss = float(abs(losses["net_pnl"].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    equity = pd.Series([initial_capital] + trades_df["capital_after_trade"].tolist())

    summary = {
        "total_trades": float(len(trades_df)),
        "win_rate": float((trades_df["net_pnl"] > 0).mean()),
        "net_pnl": float(trades_df["net_pnl"].sum()),
        "return_pct": float((capital - initial_capital) / initial_capital),
        "max_drawdown_pct": float(_compute_max_drawdown(equity)),
        "profit_factor": float(profit_factor),
    }
    return trades_df, summary
