"""Cycle gate steps."""

from __future__ import annotations

from ..context import CycleContext
from ..services import Services


def block_on_weekend(ctx: CycleContext, svc: Services) -> CycleContext:
    if ctx.now.weekday() >= 5:
        ctx.halt_cycle = True
        ctx.halt_reason = "Market closed (weekend) — no automated actions"
    return ctx


def gate_entry_window(ctx: CycleContext, svc: Services) -> CycleContext:
    if not svc.clock.in_entry_window(ctx.now):
        ctx.halt_cycle = True
    return ctx


def gate_post_guard_block(ctx: CycleContext, svc: Services) -> CycleContext:
    guards = ctx.settings.guards
    if (
        guards.block_new_entries_on_guard
        and ctx.counters.profit_guard_triggered_day == ctx.today
    ):
        ctx.actions.append("New entries blocked after profit guard trigger")
        ctx.halt_cycle = True
    return ctx


def gate_daily_trade_cap(ctx: CycleContext, svc: Services) -> CycleContext:
    max_trades = int(ctx.settings.risk.max_trades_day)
    if ctx.entries_today >= max_trades:
        ctx.actions.append(f"AUTO-BUY paused: max {max_trades} trades reached today")
        ctx.halt_cycle = True
    return ctx


def gate_max_positions(ctx: CycleContext, svc: Services) -> CycleContext:
    max_positions = int(ctx.settings.risk.max_positions)
    if ctx.open_positions >= max_positions:
        ctx.actions.append(
            f"AUTO-BUY paused: max {max_positions} open positions reached"
        )
        ctx.halt_cycle = True
    return ctx
