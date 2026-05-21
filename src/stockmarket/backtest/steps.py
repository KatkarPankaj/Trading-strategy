"""Backtest-specific cycle steps (entries and bar OHLC exits)."""

from __future__ import annotations

import pandas as pd

from stockmarket.cycle.context import CycleContext
from stockmarket.cycle.services import Services
from stockmarket.cycle.steps.entries import place_long_entries, place_short_entries

from .adapters.broker import HistoricalBroker


def backtest_place_long_entries(ctx: CycleContext, svc: Services) -> CycleContext:
    cash = float(ctx.state.cash)
    try:
        ctx.state.cash = max(cash, float(ctx.state.start_capital) * 10.0)
        return place_long_entries(ctx, svc)
    finally:
        ctx.state.cash = cash


def backtest_place_short_entries(ctx: CycleContext, svc: Services) -> CycleContext:
    cash = float(ctx.state.cash)
    try:
        ctx.state.cash = max(cash, float(ctx.state.start_capital) * 10.0)
        return place_short_entries(ctx, svc)
    finally:
        ctx.state.cash = cash


def _bar(svc: Services) -> pd.Series:
    broker = svc.broker
    if isinstance(broker, HistoricalBroker) and broker.current_bar is not None:
        return broker.current_bar
    raise RuntimeError("HistoricalBroker bar not set")


def backtest_refresh_bar(ctx: CycleContext, svc: Services) -> CycleContext:
    bar = _bar(svc)
    sym = str(ctx.settings.symbols[0])
    ctx.state.prices[sym] = float(bar.get("close", 0.0) or 0.0)
    return ctx


def backtest_bar_exits(ctx: CycleContext, svc: Services) -> CycleContext:
    broker = svc.broker
    if not isinstance(broker, HistoricalBroker):
        return ctx

    bar = _bar(svc)
    ts = ctx.now
    sym = str(ctx.settings.symbols[0])
    meta = broker.open_positions.get(sym)
    if meta is None:
        return ctx

    bar_low = float(bar.get("low", 0.0) or 0.0)
    bar_high = float(bar.get("high", 0.0) or 0.0)
    raw_close = float(bar.get("close", 0.0) or 0.0)
    square_off_t = svc.clock.square_off_time()

    exit_reason = None
    exit_price = None

    if meta.side == "long":
        if bar_low <= meta.stop_price:
            exit_reason = "stop"
            exit_price = meta.stop_price
        elif bar_high >= meta.target_price:
            exit_reason = "target"
            exit_price = meta.target_price
    else:
        if bar_high >= meta.stop_price:
            exit_reason = "stop"
            exit_price = meta.stop_price
        elif bar_low <= meta.target_price:
            exit_reason = "target"
            exit_price = meta.target_price

    if exit_reason is None and ts >= meta.time_exit_ts:
        exit_reason = "time"
        exit_price = raw_close

    if exit_reason is None and ts.time() >= square_off_t:
        exit_reason = "square_off"
        exit_price = raw_close

    if exit_reason is None:
        return ctx

    side_u = "SELL" if meta.side == "long" else "COVER"
    broker.execute(
        ctx.state,
        symbol=sym,
        side=side_u,  # type: ignore[arg-type]
        qty=meta.qty,
        price=float(exit_price),
        reason=exit_reason,
        when=ts,
    )
    return ctx


def backtest_force_day_end(ctx: CycleContext, svc: Services) -> CycleContext:
    broker = svc.broker
    if not isinstance(broker, HistoricalBroker):
        return ctx
    bar = _bar(svc)
    sym = str(ctx.settings.symbols[0])
    if sym not in broker.open_positions:
        return ctx
    raw_close = float(bar.get("close", 0.0) or 0.0)
    broker.close_at_price(
        ctx.state,
        symbol=sym,
        when=ctx.now,
        raw_close=raw_close,
        reason="forced_day_end",
    )
    return ctx
