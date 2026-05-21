"""Historical broker with legacy slippage and open-position metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from stockmarket.config import TradingConfig
from stockmarket.cycle.adapters.streamlit_broker import StreamlitBroker
from stockmarket.domain.types import PaperState, Side, TradeLogEntry


@dataclass
class OpenPositionMeta:
    side: str
    entry_ts: datetime
    entry_price: float
    qty: int
    stop_price: float
    target_price: float
    time_exit_ts: datetime


class HistoricalBroker(StreamlitBroker):
    def __init__(self, cfg: TradingConfig, charges_fn):
        super().__init__(charges_fn)
        self._cfg = cfg
        self._open: dict[str, OpenPositionMeta] = {}
        self.current_bar: pd.Series | None = None

    def set_bar(self, row: pd.Series) -> None:
        self.current_bar = row

    @property
    def open_positions(self) -> dict[str, OpenPositionMeta]:
        return self._open

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
        slip = float(self._cfg.slippage_pct)
        sl = float(sl_pct if sl_pct is not None else self._cfg.stop_loss_pct)
        tp = float(tp_pct if tp_pct is not None else self._cfg.take_profit_pct)

        if side_u == "BUY":
            px = float(price) * (1.0 + slip)
            entry = super().execute(
                state,
                symbol=sym,
                side="BUY",
                qty=qty,
                price=px,
                reason=reason,
                when=when,
                sl_pct=sl,
                tp_pct=tp,
            )
            if entry is None:
                return None
            self._open[sym] = OpenPositionMeta(
                side="long",
                entry_ts=when,
                entry_price=px,
                qty=int(qty),
                stop_price=px * (1.0 - sl),
                target_price=px * (1.0 + tp),
                time_exit_ts=when + timedelta(minutes=int(self._cfg.time_exit_minutes)),
            )
            return entry

        if side_u == "SHORT":
            px = float(price) * (1.0 - slip)
            entry = super().execute(
                state,
                symbol=sym,
                side="SHORT",
                qty=qty,
                price=px,
                reason=reason,
                when=when,
                sl_pct=sl,
                tp_pct=tp,
            )
            if entry is None:
                return None
            self._open[sym] = OpenPositionMeta(
                side="short",
                entry_ts=when,
                entry_price=px,
                qty=int(qty),
                stop_price=px * (1.0 + sl),
                target_price=px * (1.0 - tp),
                time_exit_ts=when + timedelta(minutes=int(self._cfg.time_exit_minutes)),
            )
            return entry

        meta = self._open.get(sym)
        if meta is None:
            return super().execute(
                state,
                symbol=sym,
                side=side,
                qty=qty,
                price=price,
                reason=reason,
                when=when,
            )

        exit_px = self._slipped_exit_price(meta.side, float(price), reason)
        entry = super().execute(
            state,
            symbol=sym,
            side=side,
            qty=qty,
            price=exit_px,
            reason=reason,
            when=when,
        )
        if entry is not None:
            self._open.pop(sym, None)
        return entry

    def _slipped_exit_price(self, side: str, raw: float, reason: str) -> float:
        slip = float(self._cfg.slippage_pct)
        reason_l = reason.lower()
        if side == "long":
            if "stop" in reason_l or reason_l == "stop":
                return raw * (1.0 - slip)
            if "target" in reason_l or "tp" in reason_l:
                return raw * (1.0 - slip)
            return raw * (1.0 - slip)
        if "stop" in reason_l:
            return raw * (1.0 + slip)
        if "target" in reason_l or "tp" in reason_l:
            return raw * (1.0 + slip)
        return raw * (1.0 + slip)

    def close_at_price(
        self,
        state: PaperState,
        *,
        symbol: str,
        when: datetime,
        raw_close: float,
        reason: str,
    ) -> TradeLogEntry | None:
        meta = self._open.get(symbol)
        if meta is None:
            return None
        side_u = "SELL" if meta.side == "long" else "COVER"
        slip = float(self._cfg.slippage_pct)
        if meta.side == "long":
            exit_px = raw_close * (1.0 - slip)
        else:
            exit_px = raw_close * (1.0 + slip)
        return self.execute(
            state,
            symbol=symbol,
            side=side_u,  # type: ignore[arg-type]
            qty=meta.qty,
            price=exit_px,
            reason=reason,
            when=when,
        )
