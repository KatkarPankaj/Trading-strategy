"""Exchange trading calendar: timezone, regular hours, holidays, early closes and weekend rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    timezone: str
    open_time: time
    close_time: time
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = field(default_factory=dict)
    closed_weekdays: frozenset[int] = frozenset({5, 6})  # Monday=0
    # None means "trust the holiday list"; a set means only those years are known, others fail closed.
    covered_years: frozenset[int] | None = None

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if self.open_time >= self.close_time:
            raise ValueError("open_time must be before close_time")
        for day, close in self.early_closes.items():
            if not self.open_time < close <= self.close_time:
                raise ValueError(
                    f"early close for {day} must fall inside regular hours")

    def is_trading_day(self, day: date) -> bool:
        if self.covered_years is not None and day.year not in self.covered_years:
            return False
        return day.weekday() not in self.closed_weekdays and day not in self.holidays

    def close_time_on(self, day: date) -> time:
        return self.early_closes.get(day, self.close_time)

    def is_open(self, at: datetime) -> bool:
        """True while a bar stamped `at` (bar start) lies inside that day's trading hours."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = at.astimezone(ZoneInfo(self.timezone))
        if not self.is_trading_day(local.date()):
            return False
        return self.open_time <= local.time().replace(tzinfo=None) < self.close_time_on(local.date())
