"""Entry cooldown policy."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from stockmarket.domain.types import CycleSettings


class DefaultCooldownPolicy:
    def block_reason(
        self,
        symbol: str,
        *,
        side: Literal["long", "short"],
        ref_price: float,
        sl_events: dict[str, tuple[datetime, str]],
        last_exits: dict[str, tuple[datetime, float]],
        now_naive: datetime,
        settings: CycleSettings,
    ) -> str | None:
        sym = str(symbol)
        guards = settings.guards
        prefix = "BUY" if side == "long" else "SHORT"

        sl_minutes = int(guards.sl_cooldown_after_stop_minutes)
        if sl_minutes > 0:
            last_sl = sl_events.get(sym)
            if last_sl is not None:
                sl_ts, sl_reason = last_sl
                minutes_since_sl = (now_naive - sl_ts).total_seconds() / 60.0
                if minutes_since_sl < float(sl_minutes):
                    return (
                        f"{prefix} {sym} SKIPPED: SL cooldown {minutes_since_sl:.1f}m "
                        f"< {sl_minutes}m ({sl_reason})"
                    )

        last_exit = last_exits.get(sym)
        if last_exit is None:
            return None

        exit_ts, exit_price = last_exit
        cooldown = int(guards.reentry_cooldown_minutes)
        if cooldown > 0:
            minutes_since_exit = (now_naive - exit_ts).total_seconds() / 60.0
            if minutes_since_exit < float(cooldown):
                return (
                    f"{prefix} {sym} SKIPPED: cooldown {minutes_since_exit:.1f}m "
                    f"< {cooldown}m"
                )

        min_move = float(guards.reentry_min_move_pct)
        if min_move > 0 and exit_price > 0:
            price_now = float(ref_price)
            move_pct = (
                abs(price_now - exit_price) / exit_price * 100.0 if price_now > 0 else 0.0
            )
            if move_pct < min_move:
                return (
                    f"{prefix} {sym} SKIPPED: re-entry move {move_pct:.2f}% "
                    f"< {min_move:.2f}%"
                )
        return None
