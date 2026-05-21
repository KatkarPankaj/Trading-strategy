"""Paper broker that mutates PaperState in memory."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from stockmarket.domain.types import PaperState, Position, Side, TradeLogEntry

_DEFAULT_SL = 0.008
_DEFAULT_TP = 0.016


class StreamlitBroker:
    def __init__(self, charges_fn):
        self._charges = charges_fn

    def execute(
        self,
        state: PaperState,
        *,
        symbol: str,
        side: Side,
        qty: int,
        price: float,
        reason: str,
        when: datetime,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
    ) -> TradeLogEntry | None:
        sym = str(symbol)
        side_u = str(side).upper()
        q = int(qty)
        px = float(price)
        if q <= 0 or px <= 0:
            return None

        sl = float(sl_pct if sl_pct is not None else _DEFAULT_SL)
        tp = float(tp_pct if tp_pct is not None else _DEFAULT_TP)
        value = float(q) * px
        ch = float(self._charges(side_u, value))
        realized_delta = 0.0
        holdings = deepcopy(state.holdings)
        shorts = deepcopy(state.shorts)

        log_qty = q
        if side_u == "BUY":
            state.cash -= value + ch
            pos = holdings.get(sym, Position(0, 0.0, 0.0, 0.0))
            old_qty, old_avg = int(pos.qty), float(pos.avg)
            new_qty = old_qty + q
            new_avg = ((old_qty * old_avg) + value) / max(new_qty, 1)
            holdings[sym] = Position(
                qty=new_qty,
                avg=new_avg,
                stop=new_avg * (1.0 - sl),
                target=new_avg * (1.0 + tp),
            )
        elif side_u == "SELL":
            pos = holdings.get(sym)
            if pos is None or int(pos.qty) <= 0:
                return None
            old_qty, old_avg = int(pos.qty), float(pos.avg)
            exit_qty = min(old_qty, q)
            log_qty = exit_qty
            pnl = (px - old_avg) * float(exit_qty)
            state.realized += pnl
            realized_delta = float(pnl)
            state.cash += float(exit_qty) * px - ch
            remain = old_qty - exit_qty
            if remain > 0:
                holdings[sym] = Position(
                    qty=remain,
                    avg=old_avg,
                    stop=float(pos.stop),
                    target=float(pos.target),
                )
            else:
                holdings.pop(sym, None)
        elif side_u == "SHORT":
            state.cash += value - ch
            pos = shorts.get(sym, Position(0, 0.0, 0.0, 0.0))
            old_qty, old_avg = int(pos.qty), float(pos.avg)
            new_qty = old_qty + q
            new_avg = ((old_qty * old_avg) + value) / max(new_qty, 1)
            shorts[sym] = Position(
                qty=new_qty,
                avg=new_avg,
                stop=new_avg * (1.0 + sl),
                target=new_avg * (1.0 - tp),
            )
        elif side_u == "COVER":
            pos = shorts.get(sym)
            if pos is None or int(pos.qty) <= 0:
                return None
            old_qty, old_avg = int(pos.qty), float(pos.avg)
            cover_qty = min(old_qty, q)
            log_qty = cover_qty
            pnl = (old_avg - px) * float(cover_qty)
            state.realized += pnl
            realized_delta = float(pnl)
            state.cash -= float(cover_qty) * px + ch
            remain = old_qty - cover_qty
            if remain > 0:
                shorts[sym] = Position(
                    qty=remain,
                    avg=old_avg,
                    stop=float(pos.stop),
                    target=float(pos.target),
                )
            else:
                shorts.pop(sym, None)
        else:
            return None

        state.holdings = holdings
        state.shorts = shorts
        state.charges += ch
        state.prices[sym] = px
        entry = TradeLogEntry(
            ts=when.strftime("%Y-%m-%d %H:%M:%S"),
            symbol=sym,
            side=side_u,  # type: ignore[arg-type]
            qty=log_qty,
            price=px,
            charges=ch,
            realized_delta=realized_delta,
            reason=reason,
            cash_after=float(state.cash),
        )
        state.log.append(entry)
        return entry
