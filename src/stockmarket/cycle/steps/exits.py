"""Exit and risk-management steps."""

from __future__ import annotations

from ..context import CycleContext
from ..services import Services
from .counters import _today_realized


def evaluate_profit_ladder(ctx: CycleContext, svc: Services) -> CycleContext:
    guards = ctx.settings.guards
    ladder_target = float(guards.daily_profit_target)
    if ladder_target <= 0.0 or ctx.counters.profit_ladder_exited_day == ctx.today:
        return ctx

    current_open_pnl = svc.history.current_open_pnl(ctx.state)
    daily_pnl_now = float(_today_realized(ctx) + current_open_pnl)
    ladder_high = ladder_target + 2000.0
    ladder_floor = ladder_target + 1000.0

    if (not ctx.counters.profit_ladder_armed) and daily_pnl_now >= ladder_target:
        ctx.counters.profit_ladder_armed = True
        ctx.counters.profit_ladder_pullback_started = False
        ctx.actions.append(
            f"Profit ladder armed at Rs {ladder_target:,.0f}; aiming Rs {ladder_high:,.0f}, "
            f"lock Rs {ladder_floor:,.0f}"
        )

    if not ctx.counters.profit_ladder_armed:
        return ctx

    should_exit = False
    ladder_reason = ""
    if daily_pnl_now >= ladder_high:
        should_exit = True
        ladder_reason = f"Profit ladder target hit @ Rs {daily_pnl_now:,.2f}"
    else:
        if daily_pnl_now < (ladder_high - 1.0):
            ctx.counters.profit_ladder_pullback_started = True
        if ctx.counters.profit_ladder_pullback_started and daily_pnl_now <= ladder_floor:
            should_exit = True
            ladder_reason = f"Profit ladder lock exit @ Rs {daily_pnl_now:,.2f}"

    if not should_exit:
        return ctx

    for sym, pos in list(ctx.state.holdings.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state,
            symbol=sym,
            side="SELL",
            qty=qty,
            price=ltp,
            reason="Profit ladder exit",
            when=ctx.now,
        )
        ctx.actions.append(f"SELL {sym}: profit ladder @ Rs {ltp:.2f}")

    for sym, pos in list(ctx.state.shorts.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state,
            symbol=sym,
            side="COVER",
            qty=qty,
            price=ltp,
            reason="Profit ladder exit",
            when=ctx.now,
        )
        ctx.actions.append(f"COVER {sym}: profit ladder @ Rs {ltp:.2f}")

    ctx.counters.profit_ladder_exited_day = ctx.today
    ctx.counters.profit_ladder_armed = False
    ctx.counters.profit_ladder_pullback_started = False
    ctx.actions.append(ladder_reason)
    ctx.halt_cycle = True
    return ctx


def force_exits_long(ctx: CycleContext, svc: Services) -> CycleContext:
    sl_pct = float(ctx.settings.risk.sl_pct)
    tp_pct = float(ctx.settings.risk.tp_pct)
    for sym, pos in list(ctx.state.holdings.items()):
        qty, avg = int(pos.qty), float(pos.avg)
        ltp = float(ctx.state.prices.get(sym, avg))
        if qty <= 0 or avg <= 0 or ltp <= 0:
            continue
        stop_price = float(pos.stop if pos.stop else avg * (1.0 - sl_pct))
        target_price = float(pos.target if pos.target else avg * (1.0 + tp_pct))
        if ltp <= stop_price:
            svc.broker.execute(
                ctx.state, symbol=sym, side="SELL", qty=qty, price=ltp,
                reason="Auto SL", when=ctx.now,
            )
            ctx.same_cycle_exited.add(sym)
            ctx.actions.append(f"SELL {sym}: SL hit @ Rs {ltp:.2f}")
        elif ltp >= target_price:
            svc.broker.execute(
                ctx.state, symbol=sym, side="SELL", qty=qty, price=ltp,
                reason="Auto TP", when=ctx.now,
            )
            ctx.same_cycle_exited.add(sym)
            ctx.actions.append(f"SELL {sym}: TP hit @ Rs {ltp:.2f}")
    return ctx


def force_exits_short(ctx: CycleContext, svc: Services) -> CycleContext:
    sl_pct = float(ctx.settings.risk.sl_pct)
    tp_pct = float(ctx.settings.risk.tp_pct)
    for sym, pos in list(ctx.state.shorts.items()):
        qty, avg = int(pos.qty), float(pos.avg)
        ltp = float(ctx.state.prices.get(sym, avg))
        if qty <= 0 or avg <= 0 or ltp <= 0:
            continue
        stop_price = float(pos.stop if pos.stop else avg * (1.0 + sl_pct))
        target_price = float(pos.target if pos.target else avg * (1.0 - tp_pct))
        if ltp >= stop_price:
            svc.broker.execute(
                ctx.state, symbol=sym, side="COVER", qty=qty, price=ltp,
                reason="Auto short SL", when=ctx.now,
            )
            ctx.same_cycle_exited.add(sym)
            ctx.actions.append(f"COVER {sym}: short SL hit @ Rs {ltp:.2f}")
        elif ltp <= target_price:
            svc.broker.execute(
                ctx.state, symbol=sym, side="COVER", qty=qty, price=ltp,
                reason="Auto short TP", when=ctx.now,
            )
            ctx.same_cycle_exited.add(sym)
            ctx.actions.append(f"COVER {sym}: short TP hit @ Rs {ltp:.2f}")
    return ctx


def signal_exits(ctx: CycleContext, svc: Services) -> CycleContext:
    signals = ctx.settings.signals
    if not signals.enable_signal_sell or ctx.signals.sell_exit_df.empty:
        return ctx

    signal_exits_done = 0
    for _, row in ctx.signals.sell_exit_df.iterrows():
        if signal_exits_done >= int(signals.max_signal_exits_per_cycle):
            break
        sym = str(row.get("symbol", ""))
        if sym not in ctx.state.holdings:
            continue
        if str(row.get("sell_signal", "WAIT")) != "READY":
            continue
        score = float(
            row.get("effective_sell_score", row.get("sell_score", 0.0)) or 0.0
        )
        if score < float(signals.min_sell_score):
            continue
        pos = ctx.state.holdings[sym]
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, row.get("price", 0.0) or 0.0))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state,
            symbol=sym,
            side="SELL",
            qty=qty,
            price=ltp,
            reason=f"Auto SELL signal (score {score:.1f})",
            when=ctx.now,
        )
        ctx.same_cycle_exited.add(sym)
        ctx.actions.append(f"SELL {sym}: signal READY @ Rs {ltp:.2f} (score {score:.1f})")
        signal_exits_done += 1
    return ctx


def evaluate_profit_guard(ctx: CycleContext, svc: Services) -> CycleContext:
    guards = ctx.settings.guards
    if (
        not guards.enable_profit_guard
        or ctx.now.time() < guards.profit_guard_after
        or float(ctx.counters.peak_open_pnl) <= 0.0
        or ctx.counters.profit_guard_triggered_day == ctx.today
    ):
        return ctx

    peak = float(ctx.counters.peak_open_pnl)
    now_pnl = svc.history.current_open_pnl(ctx.state)
    drawdown_pct = ((peak - now_pnl) / peak) * 100.0
    if drawdown_pct < float(guards.profit_guard_drawdown_pct):
        return ctx

    for sym, pos in list(ctx.state.holdings.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state, symbol=sym, side="SELL", qty=qty, price=ltp,
            reason="Profit guard square-off", when=ctx.now,
        )
        ctx.same_cycle_exited.add(sym)
        ctx.actions.append(f"SELL {sym}: profit guard @ Rs {ltp:.2f}")

    for sym, pos in list(ctx.state.shorts.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state, symbol=sym, side="COVER", qty=qty, price=ltp,
            reason="Profit guard square-off", when=ctx.now,
        )
        ctx.same_cycle_exited.add(sym)
        ctx.actions.append(f"COVER {sym}: profit guard @ Rs {ltp:.2f}")

    ctx.counters.profit_guard_triggered_day = ctx.today
    ctx.actions.append(
        f"Profit guard triggered: open PnL drawdown {drawdown_pct:.1f}% from peak"
    )
    return ctx


def intraday_square_off(ctx: CycleContext, svc: Services) -> CycleContext:
    if ctx.now.time() < svc.clock.square_off_time():
        return ctx

    for sym, pos in list(ctx.state.holdings.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state, symbol=sym, side="SELL", qty=qty, price=ltp,
            reason="Auto square-off", when=ctx.now,
        )
        ctx.actions.append(f"SELL {sym}: square-off @ Rs {ltp:.2f}")

    for sym, pos in list(ctx.state.shorts.items()):
        qty = int(pos.qty)
        if qty <= 0:
            continue
        ltp = float(ctx.state.prices.get(sym, pos.avg))
        if ltp <= 0:
            continue
        svc.broker.execute(
            ctx.state, symbol=sym, side="COVER", qty=qty, price=ltp,
            reason="Auto square-off", when=ctx.now,
        )
        ctx.actions.append(f"COVER {sym}: square-off @ Rs {ltp:.2f}")

    ctx.halt_cycle = True
    return ctx
