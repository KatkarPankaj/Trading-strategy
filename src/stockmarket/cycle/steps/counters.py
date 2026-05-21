"""Daily counter steps."""

from __future__ import annotations

from ..context import CycleContext
from ..services import Services


def roll_daily_counters(ctx: CycleContext, svc: Services) -> CycleContext:
    if ctx.counters.peak_open_pnl_day != ctx.today:
        ctx.counters.peak_open_pnl_day = ctx.today
        ctx.counters.day = ctx.today
        ctx.counters.peak_open_pnl = 0.0
        ctx.counters.profit_guard_triggered_day = ""

    if ctx.counters.profit_ladder_day != ctx.today:
        ctx.counters.profit_ladder_day = ctx.today
        ctx.counters.profit_ladder_armed = False
        ctx.counters.profit_ladder_pullback_started = False
        ctx.counters.profit_ladder_exited_day = ""
    return ctx


def update_peak_open_pnl(ctx: CycleContext, svc: Services) -> CycleContext:
    current_open_pnl = svc.history.current_open_pnl(ctx.state)
    ctx.counters.peak_open_pnl = max(float(ctx.counters.peak_open_pnl), current_open_pnl)
    return ctx


def _today_realized(ctx: CycleContext) -> float:
    return sum(
        float(row.realized_delta)
        for row in ctx.state.log
        if str(row.ts).startswith(ctx.today)
    )
