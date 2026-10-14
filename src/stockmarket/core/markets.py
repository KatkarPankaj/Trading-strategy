"""Market definitions: exchange identity, currency, timezone, sessions and holiday data, per market."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

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
    exchange_calendars: Mapping[str, TradingCalendar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.mics or len(self.currency) != 3:
            raise ValueError(
                "market needs a code, at least one MIC and a 3-letter currency")
        if self.pre_market_open is not None and self.pre_market_open >= self.calendar.open_time:
            raise ValueError("pre_market_open must precede the regular open")
        if self.post_market_close is not None and self.post_market_close <= self.calendar.close_time:
            raise ValueError("post_market_close must follow the regular close")
        if self.exchange_calendars and set(self.exchange_calendars) != set(self.mics):
            raise ValueError("exchange calendars must cover every registered MIC")
        if any(cal.timezone != self.calendar.timezone or
               (cal.open_time, cal.close_time) != (self.calendar.open_time, self.calendar.close_time)
               for cal in self.exchange_calendars.values()):
            raise ValueError("exchange overrides must retain market timezone and regular hours")
        object.__setattr__(self, "exchange_calendars", MappingProxyType(dict(self.exchange_calendars)))

    def close_time_on(self, day: date) -> time:
        """Country-wide schedulers flatten conservatively at the earliest venue close."""
        return min(cal.close_time_on(day) for cal in (
            tuple(self.exchange_calendars.values()) or (self.calendar,)))

    @property
    def timezone(self) -> str:
        return self.calendar.timezone

    def is_covered(self, day: date) -> bool:
        """False when no holiday data exists for the day's year; such days are treated as closed."""
        if self.exchange_calendars:
            return all(cal.is_covered(day) for cal in self.exchange_calendars.values())
        return self.calendar.is_covered(day)

    def phase(self, at: datetime, mic: str | None = None) -> SessionPhase:
        if not isinstance(at, datetime):
            raise TypeError("market phase decisions require a datetime")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if mic is not None and mic not in self.mics:
            raise ValueError("exchange does not belong to market")
        if self.exchange_calendars and mic is None:
            phases = {self.phase(at, exchange) for exchange in self.mics}
            return phases.pop() if len(phases) == 1 else SessionPhase.CLOSED
        cal = self.exchange_calendars.get(mic, self.calendar)
        local = at.astimezone(ZoneInfo(cal.timezone))
        day, t = local.date(), local.time().replace(tzinfo=None)
        if not cal.is_trading_day(day):
            return SessionPhase.CLOSED
        if cal.is_open(at):
            return SessionPhase.REGULAR
        if day in cal.special_sessions:
            return SessionPhase.CLOSED
        close = cal.close_time_on(day)
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
            tick_size=tick_size,
            lot_size=self.default_lot_size if lot_size is None else lot_size,
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


def load_calendar_file(path: Path, *, reviewed_exchange: str | None = None) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("years"), list) \
            or any(isinstance(y, bool) or not isinstance(y, int) for y in data["years"]):
        raise ValueError("calendar must contain an explicit integer year list")
    result: dict[str, Any] = {
        "covered_years": frozenset(int(y) for y in data["years"]),
        "holidays": frozenset(date.fromisoformat(d) for d in data.get("holidays", [])),
        "early_closes": {date.fromisoformat(d): time.fromisoformat(t)
                         for d, t in data.get("early_closes", {}).items()},
        "special_sessions": {
            date.fromisoformat(day): tuple((time.fromisoformat(start), time.fromisoformat(end))
                                          for start, end in sessions)
            for day, sessions in data.get("special_sessions", {}).items()},
    }
    if reviewed_exchange is not None:
        result["review_required"] = True
        review = data.get("review")
        if review is None:
            return result  # observation configuration is accepted, execution remains closed
        if not isinstance(review, dict) or not result["holidays"] or not data["years"]:
            raise ValueError("reviewed India calendar requires nonempty holidays and coverage")
        host = {"XNSE": "nseindia.com", "XBOM": "bseindia.com"}.get(reviewed_exchange)
        if host is None or review.get("exchange") != reviewed_exchange \
                or review.get("segment") != "CASH_EQUITY":
            raise ValueError("calendar review must identify the exact exchange and cash segment")
        sources = review.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("calendar review requires official source publications")
        for source in sources:
            parsed = urlparse(source["url"])
            if parsed.scheme != "https" or not parsed.hostname \
                    or not (parsed.hostname == host or parsed.hostname.endswith("." + host)):
                raise ValueError("calendar sources must be official exchange HTTPS publications")
            artifact = Path(source["artifact"])
            if artifact.is_absolute() or ".." in artifact.parts or not (
                    path.parent / artifact).resolve().is_relative_to(path.parent.resolve()):
                raise ValueError("calendar evidence artifacts must be relative to the calendar directory")
            if hashlib.sha256((path.parent / artifact).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("calendar source artifact checksum mismatch")
        reviewed_at = datetime.fromisoformat(review["reviewed_at"])
        retrieved_at = datetime.fromisoformat(review["retrieved_at"])
        if any(t.tzinfo is None or t.utcoffset() is None for t in (reviewed_at, retrieved_at)) \
                or retrieved_at > reviewed_at or reviewed_at > datetime.now(timezone.utc):
            raise ValueError("calendar review/retrieval timestamps must be aware, ordered and not future")
        start, end = date.fromisoformat(review["coverage_start"]), date.fromisoformat(review["coverage_end"])
        if start > end or set(range(start.year, end.year + 1)) != set(data["years"]):
            raise ValueError("calendar review coverage must match declared years")
        schedule = {key: data.get(key, {} if key != "holidays" else [])
                    for key in ("years", "holidays", "early_closes", "special_sessions")}
        digest = hashlib.sha256(json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report_path = Path(review["validation_report"])
        if report_path.is_absolute() or ".." in report_path.parts or not (
                path.parent / report_path).resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("calendar validation report must be relative to the calendar directory")
        report = json.loads((path.parent / report_path).read_text(encoding="utf-8"))
        dates = sorted(set(data["holidays"]) | set(data.get("early_closes", {}))
                       | set(data.get("special_sessions", {})))
        if report.get("schedule_sha256") != digest or report.get("verified_dates") != dates \
                or report.get("status") != "REVIEWED" or not report.get("reviewer"):
            raise ValueError("calendar validation report must attest the exact schedule and every listed date")
        evidence = report.get("date_evidence", {})
        hashes = {source["sha256"] for source in sources}
        if set(evidence) != set(dates) or any(
                item.get("source_sha256") not in hashes or not item.get("locator")
                for item in evidence.values()):
            raise ValueError("each calendar date requires an official publication checksum and locator")
        if any(not start <= date.fromisoformat(d) <= end for d in dates):
            raise ValueError("calendar event outside reviewed coverage")
        expires = date.fromisoformat(review["expires_on"])
        if expires < start:
            raise ValueError("calendar review must not expire before coverage begins")
        result.update(coverage_start=start, coverage_end=end, review_expires=expires,
                      provenance={"exchange": reviewed_exchange, "segment": "CASH_EQUITY",
                                  "reviewed_at": reviewed_at.isoformat(), "schedule_sha256": digest,
                                  "validation_report": review["validation_report"]})
    return result


def _calendar(code: str, tz: str, open_: time, close: time, calendar_dir: Path) -> TradingCalendar:
    path = calendar_dir / f"{code}.json"
    # No data file means no known holidays: the market stays closed until one is supplied.
    extra = load_calendar_file(path, reviewed_exchange=code if code in {"XNSE", "XBOM"} else None) if path.is_file() else {
        "covered_years": frozenset()}
    if code in {"XNSE", "XBOM", "IN"}:
        extra["review_required"] = True
    return TradingCalendar(tz, open_, close, **extra)


def default_markets(calendar_dir: Path | None = None) -> MarketRegistry:
    """US, India and Germany (Xetra). Adding a market is one more definition plus a calendar file."""
    d = calendar_dir or CALENDAR_DIR
    registry = MarketRegistry()
    registry.register(MarketDefinition(
        "US", "United States equities and ETFs", (
            "XNYS", "XNAS", "ARCX"), "USD",
        _calendar("US", "America/New_York", time(9, 30), time(16, 0), d),
        pre_market_open=time(4, 0), post_market_close=time(20, 0),
        exchange_calendars={
            mic: _calendar(mic if (d / f"{mic}.json").is_file() else "US",
                           "America/New_York", time(9, 30), time(16, 0), d)
            for mic in ("XNYS", "XNAS", "ARCX")
        }))
    registry.register(MarketDefinition(
        "IN", "India (NSE/BSE) equities", ("XNSE", "XBOM"), "INR",
        _calendar("IN", "Asia/Kolkata", time(9, 15), time(15, 30), d),
        pre_market_open=time(9, 0),
        exchange_calendars={
            mic: _calendar(mic,
                           "Asia/Kolkata", time(9, 15), time(15, 30), d)
            for mic in ("XNSE", "XBOM")
        }))
    registry.register(MarketDefinition(
        "DE", "Germany (Xetra) equities", ("XETR",), "EUR",
        _calendar("DE", "Europe/Berlin", time(9, 0), time(17, 30), d),
        pre_market_open=time(8, 0), post_market_close=time(20, 0)))
    return registry
