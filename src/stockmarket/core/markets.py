"""Market definitions: exchange identity, currency, timezone, sessions and holiday data, per market."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import AssetClass, Instrument, TradingStatus
from .trading_calendar import TradingCalendar

CALENDAR_DIR = Path(__file__).parent / "calendars"


class UnknownMarket(KeyError):
    pass


class SessionPhase(str, Enum):
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    POST_MARKET = "POST_MARKET"


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    code: str
    name: str
    mics: tuple[str, ...]
    currency: str
    calendar: TradingCalendar
    pre_market_open: time | None = None
    post_market_close: time | None = None
    default_lot_size: int = 1

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.mics or len(self.currency) != 3:
            raise ValueError(
                "market needs a code, at least one MIC and a 3-letter currency")
        if self.pre_market_open is not None and self.pre_market_open >= self.calendar.open_time:
            raise ValueError("pre_market_open must precede the regular open")
        if self.post_market_close is not None and self.post_market_close <= self.calendar.close_time:
            raise ValueError("post_market_close must follow the regular close")

    @property
    def timezone(self) -> str:
        return self.calendar.timezone

    def is_covered(self, day: date) -> bool:
        """False when no holiday data exists for the day's year; such days are treated as closed."""
        years = self.calendar.covered_years
        return years is None or day.year in years

    def phase(self, at: datetime) -> SessionPhase:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = at.astimezone(ZoneInfo(self.timezone))
        day, t = local.date(), local.time().replace(tzinfo=None)
        cal = self.calendar
        if not cal.is_trading_day(day):
            return SessionPhase.CLOSED
        close = cal.close_time_on(day)
        if cal.open_time <= t < close:
            return SessionPhase.REGULAR
        if self.pre_market_open is not None and self.pre_market_open <= t < cal.open_time:
            return SessionPhase.PRE_MARKET
        if self.post_market_close is not None and close <= t < self.post_market_close:
            return SessionPhase.POST_MARKET
        return SessionPhase.CLOSED

    def instrument(
        self,
        symbol: str,
        *,
        mic: str,
        asset_class: AssetClass,
        tick_size: float,
        lot_size: int | None = None,
        **extra: Any,
    ) -> Instrument:
        """Build an Instrument whose currency, timezone and hours come from this market, not from the caller."""
        if mic not in self.mics:
            raise ValueError(f"{mic} is not an exchange of market {self.code}")
        return Instrument(
            instrument_id=f"{mic}:{symbol}", symbol=symbol, exchange=mic, market=self.code,
            asset_class=asset_class, currency=self.currency, timezone=self.timezone,
            tick_size=tick_size, lot_size=lot_size or self.default_lot_size,
            trading_hours=(self.calendar.open_time, self.calendar.close_time),
            trading_status=extra.pop("trading_status", TradingStatus.ACTIVE), **extra)


class MarketRegistry:
    def __init__(self) -> None:
        self._markets: dict[str, MarketDefinition] = {}

    def register(self, market: MarketDefinition) -> None:
        key = market.code.upper()
        if key in self._markets:
            raise ValueError(f"market {key} is already registered")
        self._markets[key] = market

    def get(self, code: str) -> MarketDefinition:
        try:
            return self._markets[code.upper()]
        except KeyError:
            raise UnknownMarket(
                f"unknown market {code!r}; registered: {self.codes()}") from None

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._markets))

    def phase(self, code: str, at: datetime) -> SessionPhase:
        return self.get(code).phase(at)

    def is_regular_session(self, code: str, at: datetime) -> bool:
        """Unknown markets are reported closed rather than raising, so callers fail closed."""
        try:
            return self.get(code).phase(at) is SessionPhase.REGULAR
        except UnknownMarket:
            return False


def load_calendar_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "covered_years": frozenset(int(y) for y in data["years"]),
        "holidays": frozenset(date.fromisoformat(d) for d in data.get("holidays", [])),
        "early_closes": {date.fromisoformat(d): time.fromisoformat(t)
                         for d, t in data.get("early_closes", {}).items()},
    }


def _calendar(code: str, tz: str, open_: time, close: time, calendar_dir: Path) -> TradingCalendar:
    path = calendar_dir / f"{code}.json"
    # No data file means no known holidays: the market stays closed until one is supplied.
    extra = load_calendar_file(path) if path.is_file() else {
        "covered_years": frozenset()}
    return TradingCalendar(tz, open_, close, **extra)


def default_markets(calendar_dir: Path | None = None) -> MarketRegistry:
    """US, India and Germany (Xetra). Adding a market is one more definition plus a calendar file."""
    d = calendar_dir or CALENDAR_DIR
    registry = MarketRegistry()
    registry.register(MarketDefinition(
        "US", "United States equities and ETFs", (
            "XNYS", "XNAS", "ARCX"), "USD",
        _calendar("US", "America/New_York", time(9, 30), time(16, 0), d),
        pre_market_open=time(4, 0), post_market_close=time(20, 0)))
    registry.register(MarketDefinition(
        "IN", "India (NSE/BSE) equities", ("XNSE", "XBOM"), "INR",
        _calendar("IN", "Asia/Kolkata", time(9, 15), time(15, 30), d),
        pre_market_open=time(9, 0)))
    registry.register(MarketDefinition(
        "DE", "Germany (Xetra) equities", ("XETR",), "EUR",
        _calendar("DE", "Europe/Berlin", time(9, 0), time(17, 30), d),
        pre_market_open=time(8, 0), post_market_close=time(20, 0)))
    return registry
