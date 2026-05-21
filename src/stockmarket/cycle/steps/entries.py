"""Entry placement steps."""

from __future__ import annotations

from ..context import CycleContext
from ..services import Services


def _regime_flags(settings) -> tuple[str, bool, bool]:
    regime = str(settings.guards.market_regime or "unknown").strip().lower()
    allow_buy = regime in {"bullish", "mixed", "unknown"}
    allow_short = regime in {"bearish", "mixed", "unknown"}
    return regime, allow_buy, allow_short


def place_long_entries(ctx: CycleContext, svc: Services) -> CycleContext:
    settings = ctx.settings
    signals = settings.signals
    guards = settings.guards
    risk = settings.risk
    regime, allow_buy, _ = _regime_flags(settings)
    now_naive = ctx.now.replace(tzinfo=None)

    if guards.enable_regime_entry_gate and not allow_buy:
        ctx.actions.append(f"BUY entries blocked by regime gate ({regime})")

    sl_events = svc.history.latest_stop_loss_by_symbol(ctx.state)
    max_trades = int(risk.max_trades_day)
    max_positions = int(risk.max_positions)

    while (
        ctx.entries_today < max_trades
        and ctx.open_positions < max_positions
        and (not guards.enable_regime_entry_gate or allow_buy)
    ):
        ranked = svc.signals.rank(ctx.state, settings)
        live_buy_df = ranked.buy_df
        if live_buy_df.empty:
            break

        traded = False
        idle_minutes = svc.history.minutes_since_last_entry(ctx.state, ctx.now, ctx.today)
        fallback_active = svc.idle_fallback.allow_below_min_score(
            idle_minutes=idle_minutes, settings=settings
        )
        latest_exits = svc.history.latest_exit_by_symbol(ctx.state, ctx.today)

        for _, row in live_buy_df.iterrows():
            if ctx.entries_today >= max_trades or ctx.open_positions >= max_positions:
                break

            sym = str(row["symbol"])
            if sym in ctx.same_cycle_entered:
                ctx.actions.append(f"BUY {sym} SKIPPED: already entered this cycle")
                continue
            if sym in ctx.state.holdings or sym in ctx.state.shorts:
                continue

            block = svc.cooldown.block_reason(
                sym,
                side="long",
                ref_price=float(row.get("price", 0.0) or 0.0),
                sl_events=sl_events,
                last_exits=latest_exits,
                now_naive=now_naive,
                settings=settings,
            )
            if block:
                ctx.actions.append(block)
                continue
            if sym in ctx.same_cycle_exited:
                ctx.actions.append(f"BUY {sym} SKIPPED: same-cycle re-entry blocked")
                continue
            if str(row.get("buy_signal", "WAIT")) != "READY":
                continue

            price = float(row.get("price", 0.0) or 0.0)
            score = float(
                row.get("effective_buy_score", row.get("buy_score", 0.0)) or 0.0
            )
            if price <= 0:
                continue
            if score < float(signals.min_buy_score) and not fallback_active:
                ctx.actions.append(
                    f"BUY {sym} SKIPPED: score {score:.1f} < min {float(signals.min_buy_score):.1f}"
                )
                continue

            qty = svc.sizer.size_long(price=price, state=ctx.state, settings=settings)
            if qty <= 0:
                ctx.actions.append(f"BUY {sym} SKIPPED: insufficient cash/risk budget")
                continue

            est_value = qty * price
            est_ch = svc.charges_fn("BUY", est_value)
            if ctx.state.cash < (est_value + est_ch):
                ctx.actions.append(f"BUY {sym} SKIPPED: insufficient cash")
                continue

            buy_reason = "Auto BUY signal"
            if score < float(signals.min_buy_score) and fallback_active:
                buy_reason = (
                    f"Auto BUY fallback after {int(signals.idle_buy_fallback_minutes)}m idle"
                )
                ctx.actions.append(
                    f"BUY {sym} fallback: idle {idle_minutes:.1f}m, score {score:.1f} "
                    f"< min {float(signals.min_buy_score):.1f}"
                )

            svc.broker.execute(
                ctx.state,
                symbol=sym,
                side="BUY",
                qty=qty,
                price=price,
                reason=buy_reason,
                when=ctx.now,
                sl_pct=risk.sl_pct,
                tp_pct=risk.tp_pct,
            )
            ctx.actions.append(f"BUY {sym} {qty}qty @ Rs {price:.2f}")
            ctx.same_cycle_entered.add(sym)
            ctx.entries_today += 1
            ctx.open_positions += 1
            traded = True
            break

        if not traded:
            break
    return ctx


def place_short_entries(ctx: CycleContext, svc: Services) -> CycleContext:
    settings = ctx.settings
    signals = settings.signals
    guards = settings.guards
    risk = settings.risk
    regime, _, allow_short = _regime_flags(settings)
    now_naive = ctx.now.replace(tzinfo=None)

    if guards.enable_regime_entry_gate and not allow_short:
        ctx.actions.append(f"SHORT entries blocked by regime gate ({regime})")

    if not signals.enable_short_selling:
        return ctx
    if guards.enable_regime_entry_gate and not allow_short:
        return ctx

    sl_events = svc.history.latest_stop_loss_by_symbol(ctx.state)
    max_trades = int(risk.max_trades_day)
    max_positions = int(risk.max_positions)

    while ctx.entries_today < max_trades and ctx.open_positions < max_positions:
        ranked = svc.signals.rank(ctx.state, settings)
        live_sell_df = ranked.sell_df
        if live_sell_df.empty:
            break

        traded = False
        latest_exits = svc.history.latest_exit_by_symbol(ctx.state, ctx.today)

        for _, row in live_sell_df.iterrows():
            if ctx.entries_today >= max_trades or ctx.open_positions >= max_positions:
                break

            sym = str(row.get("symbol", ""))
            if sym in ctx.same_cycle_entered:
                ctx.actions.append(f"SHORT {sym} SKIPPED: already entered this cycle")
                continue
            if sym in ctx.state.holdings or sym in ctx.state.shorts:
                continue

            block = svc.cooldown.block_reason(
                sym,
                side="short",
                ref_price=float(row.get("price", 0.0) or 0.0),
                sl_events=sl_events,
                last_exits=latest_exits,
                now_naive=now_naive,
                settings=settings,
            )
            if block:
                ctx.actions.append(block)
                continue
            if sym in ctx.same_cycle_exited:
                ctx.actions.append(f"SHORT {sym} SKIPPED: same-cycle re-entry blocked")
                continue
            if str(row.get("sell_signal", "WAIT")) != "READY":
                continue

            price = float(row.get("price", 0.0) or 0.0)
            score = float(
                row.get("effective_sell_score", row.get("sell_score", 0.0)) or 0.0
            )
            if price <= 0 or score < float(signals.min_short_score):
                continue

            qty = svc.sizer.size_short(price=price, state=ctx.state, settings=settings)
            if qty <= 0:
                ctx.actions.append(f"SHORT {sym} SKIPPED: insufficient cash/risk budget")
                continue

            est_value = qty * price
            est_ch = svc.charges_fn("SELL", est_value)
            est_margin = est_value * 0.2
            if ctx.state.cash < (est_margin + est_ch):
                ctx.actions.append(f"SHORT {sym} SKIPPED: insufficient margin cash")
                continue

            svc.broker.execute(
                ctx.state,
                symbol=sym,
                side="SHORT",
                qty=qty,
                price=price,
                reason="Auto SHORT signal",
                when=ctx.now,
                sl_pct=risk.sl_pct,
                tp_pct=risk.tp_pct,
            )
            ctx.actions.append(f"SHORT {sym} {qty}qty @ Rs {price:.2f}")
            ctx.same_cycle_entered.add(sym)
            ctx.entries_today += 1
            ctx.open_positions += 1
            traded = True
            break

        if not traded:
            break
    return ctx
