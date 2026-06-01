"""Trading cycle runner."""

from __future__ import annotations

from .context import CycleContext
from .ports import Step
from .services import Services
from .steps.counters import roll_daily_counters, update_peak_open_pnl
from .steps.entries import place_long_entries, place_short_entries
from .steps.exits import (
    evaluate_profit_guard,
    evaluate_profit_ladder,
    force_exits_long,
    force_exits_short,
    intraday_square_off,
    signal_exits,
)
from .steps.gates import (
    block_on_weekend,
    gate_daily_trade_cap,
    gate_entry_window,
    gate_max_positions,
    gate_post_guard_block,
)
from .steps.prices import refresh_holding_prices
from .steps.scoring import apply_signal_scores

DEFAULT_STEPS: tuple[Step, ...] = (
    refresh_holding_prices,
    block_on_weekend,
    roll_daily_counters,
    update_peak_open_pnl,
    apply_signal_scores,
    evaluate_profit_ladder,
    force_exits_long,
    force_exits_short,
    signal_exits,
    evaluate_profit_guard,
    intraday_square_off,
    gate_entry_window,
    gate_post_guard_block,
    gate_daily_trade_cap,
    gate_max_positions,
    place_long_entries,
    place_short_entries,
    refresh_holding_prices,
)


def run_cycle(
    ctx: CycleContext,
    svc: Services,
    steps: tuple[Step, ...] = DEFAULT_STEPS,
) -> CycleContext:
    if steps is DEFAULT_STEPS and (
        ctx.signals.buy_df.empty
        and ctx.signals.sell_df.empty
        and ctx.signals.sell_exit_df.empty
    ):
        refresh_holding_prices(ctx, svc)
        return ctx

    ctx.entries_today = svc.history.today_entry_count(ctx.state, ctx.today)
    ctx.open_positions = len(ctx.state.holdings) + len(ctx.state.shorts)

    for step in steps:
        ctx = step(ctx, svc)
        if ctx.halt_cycle:
            if ctx.halt_reason:
                ctx.actions.append(ctx.halt_reason)
            break

    svc.repo.save(ctx.state, ctx.counters)
    return ctx
