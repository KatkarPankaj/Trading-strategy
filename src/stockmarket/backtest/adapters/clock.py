"""Historical clock for backtest cycle runs."""

from __future__ import annotations

from datetime import datetime, time

from stockmarket.config import TradingConfig


class HistoricalClock:
    def __init__(self, cfg: TradingConfig):
        self._now = datetime(2000, 1, 1)
        self._entry_cutoff = time.fromisoformat(cfg.entry_cutoff_time)
        self._square_off = time.fromisoformat(cfg.square_off_time)
        self._market_open = time(9, 15)

    def set_now(self, ts: datetime) -> None:
        self._now = ts

    def now(self) -> datetime:
        return self._now

    def in_entry_window(self, now: datetime) -> bool:
        return now.time() <= self._entry_cutoff

    def square_off_time(self) -> time:
        return self._square_off

    def market_open_time(self) -> time:
        return self._market_open
