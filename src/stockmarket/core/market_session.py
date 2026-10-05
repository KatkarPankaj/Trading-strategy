"""Exchange-local session boundaries and time-window decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import TradingConfig


@dataclass(frozen=True, slots=True)
class MarketSession:
    timezone: str
    market_open: time
    opening_range_end: time
    entry_cutoff: time
    square_off: time
    market_close: time
    entry_start: time | None = None
    late_entry_start: time = time(12, 0)

    def __post_init__(self) -> None:
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("timezone must be a valid IANA timezone name")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc

        values = (
            self.market_open,
            self.opening_range_end,
            self.entry_cutoff,
            self.square_off,
            self.market_close,
            self.late_entry_start,
        )
        if any(not isinstance(value, time) or value.tzinfo is not None for value in values):
            raise ValueError(
                "session boundaries must be local timezone-naive time values")
        start = self.entry_start if self.entry_start is not None else self.opening_range_end
        if not isinstance(start, time) or start.tzinfo is not None:
            raise ValueError(
                "entry_start must be a local timezone-naive time value")
        if not (
            self.market_open <= self.opening_range_end
            and self.market_open <= start <= self.entry_cutoff
            and self.opening_range_end <= self.entry_cutoff
            <= self.square_off <= self.market_close
        ):
            raise ValueError(
                "session boundaries must be ordered within one trading day")
        if self.late_entry_start > self.entry_cutoff:
            raise ValueError("late_entry_start must not follow entry_cutoff")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def effective_entry_start(self) -> time:
        return self.entry_start if self.entry_start is not None else self.opening_range_end

    @classmethod
    def from_config(
        cls,
        config: object,
        *,
        entry_starts_at_open: bool = False,
    ) -> "MarketSession":
        """Build a session from configuration, defaulting to current NSE settings."""
        defaults = TradingConfig()
        timezone_name = str(
            getattr(config, "market_timezone", defaults.market_timezone)
        )
        market_open = _parse_time(
            getattr(config, "market_open_time", defaults.market_open_time),
            "market_open_time",
        )
        market_close = _parse_time(
            getattr(config, "market_close_time", defaults.market_close_time),
            "market_close_time",
        )
        entry_cutoff = _parse_time(
            getattr(config, "entry_cutoff_time", defaults.entry_cutoff_time),
            "entry_cutoff_time",
        )
        square_off = _parse_time(
            getattr(config, "square_off_time", defaults.square_off_time),
            "square_off_time",
        )
        late_entry_start = _parse_time(
            getattr(config, "late_entry_start_time",
                    defaults.late_entry_start_time),
            "late_entry_start_time",
        )
        opening_range_minutes = getattr(
            config, "opening_range_minutes", defaults.opening_range_minutes
        )
        if (
            isinstance(opening_range_minutes, bool)
            or not isinstance(opening_range_minutes, int)
            or opening_range_minutes <= 0
        ):
            raise ValueError(
                "opening_range_minutes must be a positive integer")
        opening_dt = datetime.combine(date(2000, 1, 1), market_open)
        range_end_dt = opening_dt + timedelta(minutes=opening_range_minutes)
        if range_end_dt.date() != opening_dt.date():
            raise ValueError("opening range must end on the market-open date")
        opening_range_end = range_end_dt.time()
        entry_start = market_open if entry_starts_at_open else opening_range_end
        return cls(
            timezone=timezone_name,
            market_open=market_open,
            opening_range_end=opening_range_end,
            entry_cutoff=entry_cutoff,
            square_off=square_off,
            market_close=market_close,
            entry_start=entry_start,
            late_entry_start=late_entry_start,
        )

    def now(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.zone)

    def local_time(self, at: datetime) -> time:
        return self._local_datetime(at).time().replace(tzinfo=None)

    def is_before_open(self, at: datetime) -> bool:
        return self.local_time(at) < self.market_open

    def is_market_open(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return self.market_open <= local_time <= self.market_close

    def is_opening_range(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return self.market_open <= local_time <= self.opening_range_end

    def is_entry_allowed(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return (
            self.is_market_open(at)
            and self.effective_entry_start <= local_time <= self.entry_cutoff
        )

    def is_before_entry_cutoff(self, at: datetime) -> bool:
        return self.local_time(at) <= self.entry_cutoff

    def is_late_entry_window(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return self.late_entry_start <= local_time <= self.entry_cutoff

    def is_late_session(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return self.entry_cutoff < local_time <= self.square_off

    def is_square_off(self, at: datetime) -> bool:
        return self.local_time(at) >= self.square_off

    def is_market_closed(self, at: datetime) -> bool:
        local_time = self.local_time(at)
        return local_time < self.market_open or local_time > self.market_close

    def is_at_or_after(self, at: datetime, local_boundary: time) -> bool:
        self._validate_local_time(local_boundary, "local_boundary")
        return self.local_time(at) >= local_boundary

    def is_between_local_times(
        self,
        at: datetime,
        start: time,
        end: time,
    ) -> bool:
        self._validate_local_time(start, "start")
        self._validate_local_time(end, "end")
        local_time = self.local_time(at)
        if start <= end:
            return start <= local_time <= end
        return local_time >= start or local_time <= end

    def is_auto_exit_window(
        self,
        at: datetime,
        *,
        minutes_before_close: int,
    ) -> bool:
        if (
            isinstance(minutes_before_close, bool)
            or not isinstance(minutes_before_close, int)
            or minutes_before_close < 0
        ):
            raise ValueError(
                "minutes_before_close must be a non-negative integer")
        local = self._local_datetime(at)
        close_at = datetime.combine(
            local.date(), self.market_close, tzinfo=self.zone)
        trigger_at = close_at - timedelta(minutes=minutes_before_close)
        return local >= trigger_at

    def _local_datetime(self, at: datetime) -> datetime:
        if not isinstance(at, datetime):
            raise TypeError("session decisions require a datetime")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError(
                "session decisions require a timezone-aware datetime")
        return at.astimezone(self.zone)

    @staticmethod
    def _validate_local_time(value: time, field_name: str) -> None:
        if not isinstance(value, time) or value.tzinfo is not None:
            raise ValueError(
                f"{field_name} must be a timezone-naive local time")


def _parse_time(value: object, field_name: str) -> time:
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be an ISO local time") from exc
    else:
        raise TypeError(f"{field_name} must be an ISO local time")
    if parsed.tzinfo is not None:
        raise ValueError(f"{field_name} must not include a timezone")
    return parsed
