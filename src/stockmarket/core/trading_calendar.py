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
    trading_pauses: tuple[tuple[time, time], ...] = ()
    # None means "trust the holiday list"; a set means only those years are known, others fail closed.
    covered_years: frozenset[int] | None = None
    special_sessions: Mapping[date, tuple[tuple[time, time], ...]] = field(default_factory=dict)
    coverage_start: date | None = None
    coverage_end: date | None = None
    review_required: bool = False
    review_expires: date | None = None
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if (self.coverage_start is None) != (self.coverage_end is None) or (
                self.coverage_start is not None and self.coverage_start > self.coverage_end):
            raise ValueError("calendar coverage boundaries must be paired and ordered")
        for day, sessions in self.special_sessions.items():
            if not isinstance(day, date) or not sessions:
                raise ValueError("special sessions require a date and explicit time intervals")
            previous = time.min
            for start, end in sessions:
                if any(not isinstance(t, time) or t.tzinfo is not None for t in (start, end)) \
                        or not previous <= start < end:
                    raise ValueError("special sessions must be ordered non-overlapping local intervals")
                previous = end
        if self.special_sessions.keys() & self.early_closes.keys():
            raise ValueError("a special-session date cannot also have a regular-session early close")
        if (
            not isinstance(self.open_time, time)
            or not isinstance(self.close_time, time)
            or self.open_time.tzinfo is not None
            or self.close_time.tzinfo is not None
        ):
            raise ValueError("session hours must be timezone-naive local times")
        if self.open_time >= self.close_time:
            raise ValueError("open_time must be before close_time")
        if any(not isinstance(day, date) for day in self.holidays):
            raise ValueError("holidays must contain date values")
        if any(
            isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6
            for weekday in self.closed_weekdays
        ):
            raise ValueError("closed_weekdays must contain weekday numbers from 0 to 6")
        if self.covered_years is not None and any(
            isinstance(year, bool) or not isinstance(year, int) or year < 1
            for year in self.covered_years
        ):
            raise ValueError("covered_years must contain positive year numbers")
        for day, close in self.early_closes.items():
            if (
                not isinstance(day, date)
                or not isinstance(close, time)
                or close.tzinfo is not None
                or not self.open_time < close <= self.close_time
            ):
                raise ValueError(
                    f"early close for {day} must fall inside regular hours")
        if not isinstance(self.trading_pauses, tuple):
            raise ValueError("trading_pauses must be a tuple of local time pairs")
        previous_end = self.open_time
        for pause in self.trading_pauses:
            if (
                not isinstance(pause, tuple)
                or len(pause) != 2
                or any(not isinstance(boundary, time) or boundary.tzinfo is not None
                       for boundary in pause)
            ):
                raise ValueError(
                    "each trading pause must be a pair of timezone-naive local times")
            start, end = pause
            if not previous_end <= start < end < self.close_time:
                raise ValueError(
                    "trading pauses must be ordered, non-overlapping, and within regular hours")
            previous_end = end
        object.__setattr__(self, "special_sessions", {
            day: tuple(tuple(interval) for interval in sessions)
            for day, sessions in self.special_sessions.items()})
        object.__setattr__(self, "provenance", dict(self.provenance))

    def is_trading_day(self, day: date) -> bool:
        if not self.is_covered(day):
            return False
        if day in self.special_sessions:
            return True
        return day.weekday() not in self.closed_weekdays and day not in self.holidays

    def is_covered(self, day: date) -> bool:
        if self.covered_years is not None and day.year not in self.covered_years:
            return False
        if self.coverage_start is not None and not self.coverage_start <= day <= self.coverage_end:
            return False
        if self.review_required and (not self.provenance or self.review_expires is None
                                     or day > self.review_expires):
            return False
        return True

    def close_time_on(self, day: date) -> time:
        if day in self.special_sessions:
            return self.special_sessions[day][-1][1]
        return self.early_closes.get(day, self.close_time)

    def is_open(self, at: datetime) -> bool:
        """True while a bar stamped `at` (bar start) lies inside that day's trading hours."""
        if not isinstance(at, datetime):
            raise TypeError("calendar decisions require a datetime")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = at.astimezone(ZoneInfo(self.timezone))
        local_time = local.time().replace(tzinfo=None)
        if not self.is_trading_day(local.date()):
            return False
        if local.date() in self.special_sessions:
            return any(start <= local_time < end for start, end in self.special_sessions[local.date()])
        if not self.open_time <= local_time < self.close_time_on(local.date()):
            return False
        return not any(start <= local_time < end for start, end in self.trading_pauses)
