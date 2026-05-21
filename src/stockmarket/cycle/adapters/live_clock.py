"""Market clock adapter."""

from __future__ import annotations

from datetime import datetime, time
from typing import Callable


class LiveClock:
    def __init__(
        self,
        now_fn: Callable[[], datetime],
        *,
        market_open: time,
        entry_cutoff: time,
        square_off: time,
    ):
        self._now_fn = now_fn
        self._market_open = market_open
        self._entry_cutoff = entry_cutoff
        self._square_off = square_off

    def now(self) -> datetime:
        return self._now_fn()

    def in_entry_window(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        t = now.time()
        return self._market_open <= t <= self._entry_cutoff

    def square_off_time(self) -> time:
        return self._square_off

    def market_open_time(self) -> time:
        return self._market_open
