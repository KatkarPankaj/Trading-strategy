"""Shared US REST cache, protected credit allocation and fail-closed capacity evidence."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from ..brokers import MarketStatus
from ..markets import MarketRegistry
from ..models import Instrument
from ..observability import AlertManager, AlertType
from .provider import (
    DataProviderError, DataQualityError, DataUnavailable, MarketDataProvider,
    ProviderTimeout, Quote, expected_provider_name, provider_error_code,
)
from .quality import validate_bars, validate_quote
from .twelvedata_budget import TwelveDataBudget


@dataclass(frozen=True)
class USMonitoringSettings:
    minute_limit: int = 8
    daily_limit: int = 800
    daily_baseline: int = 0
    baseline_known: bool = False
    credits_per_symbol: int = 1
    safety_reserve_pct: int = 30
    watch_refreshes_per_day: int = 1
    mode: str = "rest"
    evidence_at: datetime | None = None
    allocation_confirmed: bool = False
    ledger_path: str = str(Path("outputs") / "twelvedata_us_credits.db")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> USMonitoringSettings:
        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            value = int(env.get(name) or default)
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
            return value

        mode = (env.get("TWELVEDATA_MONITORING_MODE_US") or "rest").strip().lower()
        if mode not in {"rest", "websocket"}:
            raise ValueError("TWELVEDATA_MONITORING_MODE_US must be rest or websocket")
        confirmed = (env.get("TWELVEDATA_US_ALLOCATION_CONFIRMED") or "false").strip().lower()
        if confirmed not in {"true", "false"}:
            raise ValueError("TWELVEDATA_US_ALLOCATION_CONFIRMED must be true or false")
        evidence = (env.get("TWELVEDATA_QUOTA_EVIDENCE_AT_US") or "").strip()
        at = datetime.fromisoformat(evidence) if evidence else None
        if at is not None and (at.tzinfo is None or at.utcoffset() is None):
            raise ValueError("TWELVEDATA_QUOTA_EVIDENCE_AT_US must include a timezone")
        return cls(
            minute_limit=integer("TWELVEDATA_REQUESTS_PER_MINUTE", 8, 1, 6000),
            daily_limit=integer("TWELVEDATA_DAILY_CREDITS_US", 800, 1, 1000000000),
            daily_baseline=integer("TWELVEDATA_DAILY_USED_BASELINE_US", 0, 0, 1000000000),
            baseline_known=bool((env.get("TWELVEDATA_DAILY_USED_BASELINE_US") or "").strip()),
            credits_per_symbol=integer("TWELVEDATA_TIME_SERIES_CREDITS_US", 1, 1, 1000),
            safety_reserve_pct=integer("TWELVEDATA_SAFETY_RESERVE_PCT_US", 30, 30, 100),
            watch_refreshes_per_day=integer("TWELVEDATA_WATCH_REFRESHES_PER_DAY_US", 1, 0, 10000),
            mode=mode, evidence_at=at, allocation_confirmed=confirmed == "true",
            ledger_path=env.get("TWELVEDATA_CREDIT_LEDGER_US") or cls.ledger_path)


class USMonitoringFeed(MarketDataProvider):
    """One cache/budget for US research, watch refreshes, entries and protective quotes.

    Non-US operations pass through unchanged. Streaming is deliberately unsupported until
    an entitled, validated transport exists; requesting it never grants REST entry readiness.
    """

    research_only = True

    def __init__(
        self, provider: MarketDataProvider, instruments: Mapping[str, Instrument], *,
        markets: MarketRegistry, settings: USMonitoringSettings, max_age: timedelta,
        interval_seconds: float, clock: Callable[[], datetime],
    ) -> None:
        self._p = provider
        self.name = provider.name
        self.instruments = instruments
        self.markets = markets
        self.settings = settings
        self.max_age = max_age
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.budget = TwelveDataBudget(settings.ledger_path, clock=clock)
        self.required: Callable[[], set[str]] = set
        self.universe_size: Callable[[], int] = lambda: 0
        self.watched_size: Callable[[], int] = lambda: 0
        self.monitor_status: Callable[[], Mapping[str, Any]] = dict
        self.max_positions = 0
        self.max_trades = 0
        self.candidate_limit: int | None = None
        self.research_interval_minutes = 10
        self.alerts: AlertManager | None = None
        self._lock = Lock()
        self._quote_locks = {iid: Lock() for iid, i in instruments.items() if i.market == "US"}
        self._bar_locks = {iid: Lock() for iid in self._quote_locks}
        self._quotes: dict[str, tuple[Quote, float]] = {}
        self._observations: dict[str, tuple[datetime, int]] = {}
        self._errors: dict[str, tuple[DataProviderError, float]] = {}
        self._bars: OrderedDict[tuple[str, str, datetime, datetime],
                                tuple[pd.DataFrame, float]] = OrderedDict()
        self._last_success: datetime | None = None
        self._last_failure: str | None = None

    def provider_name_for(self, instrument: Instrument) -> str:
        return expected_provider_name(self._p, instrument)

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        return self._p.get_instrument(symbol, market)

    def get_market_status(self, market: str) -> MarketStatus:
        return self._p.get_market_status(market)

    def _session_seconds(self, now: datetime) -> tuple[float, float]:
        market = self.markets.get("US")
        zone = ZoneInfo(market.timezone)
        day = now.astimezone(zone).date()
        calendars = tuple(market.exchange_calendars.values()) or (market.calendar,)
        opening = min(
            calendar.special_sessions[day][0][0]
            if day in calendar.special_sessions else calendar.open_time
            for calendar in calendars)
        opened = datetime.combine(day, opening, tzinfo=zone)
        closed = datetime.combine(day, market.close_time_on(day), tzinfo=zone)
        return max(0., (closed - opened).total_seconds()), max(
            0., (closed - max(now, opened)).total_seconds())

    def _reserve(self, *, monitoring: bool) -> None:
        now = self.clock()
        if not monitoring and not self._quota_confirmed(now):
            raise DataUnavailable("US_QUOTA_OR_ISOLATED_ALLOCATION_UNCONFIRMED")
        _, remaining = self._session_seconds(now)
        count = len(self._required())
        cost = self.settings.credits_per_symbol
        multiplier = 1 + self.settings.safety_reserve_pct / 100
        self.budget.reserve(
            credits=cost, minute_limit=self.settings.minute_limit,
            daily_limit=self.settings.daily_limit, daily_baseline=self.settings.daily_baseline,
            reserved_minute=0 if monitoring else math.ceil(
                count * math.ceil(60 / self.interval_seconds) * cost * multiplier),
            reserved_day=0 if monitoring else math.ceil(
                count * math.ceil(remaining / self.interval_seconds) * cost * multiplier))

    def _quota_confirmed(self, now: datetime) -> bool:
        at = self.settings.evidence_at
        return bool(
            self.settings.allocation_confirmed and self.settings.baseline_known and at is not None
            and at <= now and at.astimezone(timezone.utc).date() == now.astimezone(timezone.utc).date())

    def _required(self) -> set[str]:
        try:
            return self.required()
        except TimeoutError:
            raise DataUnavailable("US_MONITORING_STATE_UNAVAILABLE") from None

    def _checked_quote(self, instrument: Instrument, quote: Quote) -> Quote:
        issues = validate_quote(quote, self.clock(), self.max_age).issues
        if isinstance(quote, Quote):
            if quote.instrument_id != instrument.instrument_id:
                issues += ("INSTRUMENT_MISMATCH",)
            if quote.provider != self.provider_name_for(instrument):
                issues += ("PROVIDER_MISMATCH",)
            if not instrument.is_valid_price(quote.price):
                issues += ("PRICE_OFF_TICK",)
        if issues:
            raise DataQualityError(issues)
        return quote

    def get_quote(self, instrument: Instrument) -> Quote:
        if instrument.market != "US":
            return self._p.get_quote(instrument)
        flight = self._quote_locks.get(instrument.instrument_id)
        if flight is None:
            raise DataUnavailable("unregistered US monitoring instrument")
        if not flight.acquire(timeout=0.25):
            raise ProviderTimeout("US quote request already in progress")
        try:
            now_mono = time.monotonic()
            with self._lock:
                cached = self._quotes.get(instrument.instrument_id)
                failed = self._errors.get(instrument.instrument_id)
            if failed is not None and now_mono - failed[1] < 1:
                raise failed[0]
            if cached is not None and now_mono - cached[1] < min(
                    self.interval_seconds, 30, self.max_age.total_seconds()):
                # Source timestamp, not fetch/cache time, controls freshness on every read.
                try:
                    return self._checked_quote(instrument, cached[0])
                except DataQualityError:
                    pass  # Expired cache is not a fallback; attempt a new provider read.
            try:
                self._reserve(monitoring=instrument.instrument_id in self._required())
                quote = self._checked_quote(instrument, self._p.get_quote(instrument))
                if cached is not None and quote.timestamp < cached[0].timestamp:
                    raise DataQualityError(("QUOTE_TIMESTAMP_REGRESSION",))
            except (DataProviderError, ConnectionError, TimeoutError) as exc:
                error = exc if isinstance(exc, DataProviderError) else DataUnavailable(
                    f"Twelve Data quote transport failed: {type(exc).__name__}")
                with self._lock:
                    self._errors[instrument.instrument_id] = (error, time.monotonic())
                    self._last_failure = provider_error_code(error)
                if self.alerts is not None:
                    self.alerts.raise_alert(
                        AlertType.DATA_STALE, "US position quote unavailable; protection not assured",
                        key=instrument.instrument_id, reason=provider_error_code(error))
                raise error from None
            with self._lock:
                previous = self._observations.get(instrument.instrument_id)
                if previous is not None and self.clock() - previous[0] > self.max_age:
                    previous = None
                advances = (previous[1] + int(quote.timestamp > previous[0])) if previous else 1
                self._observations[instrument.instrument_id] = (quote.timestamp, advances)
                self._quotes[instrument.instrument_id] = (quote, time.monotonic())
                self._errors.pop(instrument.instrument_id, None)
                self._last_success = self.clock()
                self._last_failure = None
            return quote
        finally:
            flight.release()

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime,
                  end: datetime) -> pd.DataFrame:
        if instrument.market != "US":
            return self._p.get_ohlcv(instrument, interval, start, end)
        flight = self._bar_locks.get(instrument.instrument_id)
        if flight is None:
            raise DataUnavailable("unregistered US research instrument")
        if not flight.acquire(timeout=0.25):
            raise ProviderTimeout("US research request already in progress")
        try:
            key = (instrument.instrument_id, interval, start, end)
            with self._lock:
                cached = self._bars.get(key)
            if cached is not None and time.monotonic() - cached[1] < 30:
                return cached[0].copy(deep=True)
            self._reserve(monitoring=False)
            bars = self._p.get_ohlcv(instrument, interval, start, end)
            report = validate_bars(bars, interval, timezone=instrument.timezone,
                                   requested_start=start, requested_end=end)
            if not report.ok:
                raise DataQualityError(report.issues)
            with self._lock:
                self._bars[key] = (bars.copy(deep=True), time.monotonic())
                self._bars.move_to_end(key)
                while len(self._bars) > 256:
                    self._bars.popitem(last=False)
            return bars
        finally:
            flight.release()

    def status(self) -> dict[str, Any]:
        now = self.clock()
        session, remaining = self._session_seconds(now)
        blockers: list[str] = []
        try:
            required = self._required()
        except DataUnavailable as exc:
            required = set()
            blockers.append(str(exc))
        slots = max(self.max_positions, len(required))
        watched, universe = self.watched_size(), self.universe_size()
        candidates = min(universe, self.candidate_limit or universe)
        cost = self.settings.credits_per_symbol
        multiplier = 1 + self.settings.safety_reserve_pct / 100
        cycles = math.ceil(session / (60 * self.research_interval_minutes))
        remaining_cycles = math.ceil(remaining / (60 * self.research_interval_minutes))
        # Scanner bars + PAPER quote, candidate research bars, selected deterministic strategy bars.
        research_per_cycle = (2 * universe + 2 * candidates) * cost
        incidental = (universe + watched * self.settings.watch_refreshes_per_day
                      + 2 * self.max_trades) * cost
        current_full = len(required) * math.ceil(session / self.interval_seconds) * cost
        configured_full = slots * math.ceil(session / self.interval_seconds) * cost
        estimated_day = math.ceil((configured_full + cycles * research_per_cycle + incidental) * multiplier)
        estimated_remaining = math.ceil((
            slots * math.ceil(remaining / self.interval_seconds) * cost
            + remaining_cycles * research_per_cycle + incidental) * multiplier)
        minute_monitoring = slots * math.ceil(60 / self.interval_seconds) * cost
        sustained_minute = math.ceil((
            minute_monitoring + research_per_cycle / self.research_interval_minutes
            + incidental / max(1, session / 60)) * multiplier)
        monitoring_and_entry_burst = math.ceil((minute_monitoring + 2 * cost) * multiplier)
        required_minute = max(sustained_minute, monitoring_and_entry_burst)
        evidence = self.settings.evidence_at
        evidence_current = evidence is not None and evidence <= now and (
            evidence.astimezone(timezone.utc).date() == now.astimezone(timezone.utc).date())
        if not self._quota_confirmed(now):
            blockers.append("US_QUOTA_OR_ISOLATED_ALLOCATION_UNCONFIRMED")
        if self.settings.mode != "rest":
            blockers.append("US_WEBSOCKET_TRANSPORT_AND_ENTITLEMENT_UNAVAILABLE")
        try:
            minute_used, day_used = self.budget.usage()
        except DataUnavailable as exc:
            minute_used = day_used = None
            blockers.append(str(exc))
        daily_remaining = (None if day_used is None else
                           self.settings.daily_limit - self.settings.daily_baseline - day_used)
        if daily_remaining is None or estimated_remaining > daily_remaining:
            blockers.append("US_DAILY_MONITORING_AND_RESEARCH_CAPACITY_INSUFFICIENT")
        if required_minute > self.settings.minute_limit:
            blockers.append("US_MINUTE_MONITORING_AND_RESEARCH_CAPACITY_INSUFFICIENT")
        if minute_used is None or minute_used + math.ceil(
                (minute_monitoring + 2 * cost) * multiplier) > self.settings.minute_limit:
            blockers.append("US_CURRENT_MINUTE_HEADROOM_INSUFFICIENT")
        with self._lock:
            quotes = dict(self._quotes)
            observations = dict(self._observations)
            errors = dict(self._errors)
            success, failure = self._last_success, self._last_failure
        rows = []
        for iid in sorted(required):
            cached = quotes.get(iid)
            quote = cached[0] if cached else None
            valid = quote is not None and validate_quote(quote, now, self.max_age).ok
            advances = observations.get(iid, (now, 0))[1]
            rows.append({
                "instrument_id": iid, "last_valid_update": quote.timestamp.isoformat() if quote else None,
                "quote_age_seconds": (now - quote.timestamp).total_seconds() if quote else None,
                "fresh": valid, "source_updates_observed": advances,
                "last_error": provider_error_code(errors[iid][0]) if iid in errors else None})
            if not valid or advances < 2 or iid in errors:
                blockers.append("US_POSITION_FEED_STALE_MISSING_OR_NOT_ESTABLISHED")
        connected = success is not None and timedelta(0) <= now - success <= self.max_age and not failure
        established = any(
            count >= 2 and iid in quotes and validate_quote(quotes[iid][0], now, self.max_age).ok
            for iid, (_, count) in observations.items())
        if not connected or not established:
            blockers.append("US_FEED_CONTINUITY_NOT_OBSERVED")
        monitor = self.monitor_status()
        if not monitor.get("entry_ready") or monitor.get("consecutive_successes", 0) < 2:
            blockers.append("US_PROTECTIVE_MONITOR_NOT_ESTABLISHED")
        if not self.markets.is_regular_session("US", now):
            blockers.append("NO_REGULAR_US_SESSION")
        if required and blockers and self.alerts is not None:
            self.alerts.raise_alert(
                AlertType.DATA_STALE, "US monitoring capacity is not verified; entries blocked",
                key="US-monitoring-capacity", blockers=sorted(set(blockers)))
        return {
            "provider": "twelvedata", "trading_mode": "PAPER",
            "monitoring_mode": "REST", "requested_mode": self.settings.mode.upper(),
            "streaming": {"supported": False, "entitlement": "UNCONFIRMED",
                          "connection": "NOT_STARTED", "connections": 0,
                          "subscriptions": [], "reconnect_attempts": 0,
                          "required_shared_connections_if_supported": 1,
                          "required_current_symbols": len(required),
                          "required_configured_symbol_slots": slots,
                          "subscription_credits_per_symbol": None, "available_symbol_slots": None},
            "connectivity": "RECENT_VALID_QUOTE" if connected else "UNKNOWN_OR_UNAVAILABLE",
            "last_valid_received_at": success.isoformat() if success else None,
            "last_error": failure, "monitoring_capacity_verified": not blockers,
            "protective_monitor": {
                "state": monitor.get("state"), "entry_ready": monitor.get("entry_ready", False),
                "cycles": monitor.get("cycles", 0), "last_error": monitor.get("last_error"),
                "consecutive_successes": monitor.get("consecutive_successes", 0),
                "last_duration_seconds": monitor.get("last_duration_seconds"),
                "latency_budget_seconds": monitor.get("latency_budget_seconds")},
            "blockers": sorted(set(blockers)), "positions_and_pending_orders": rows,
            "quota": {"evidence_current": evidence_current,
                      "allocation_confirmed": self.settings.allocation_confirmed,
                      "baseline_known": self.settings.baseline_known,
                      "source": "Operator-configured allocation; not a provider usage query",
                      "evidence_at": evidence.isoformat() if evidence else None,
                      "minute_limit": self.settings.minute_limit, "daily_limit": self.settings.daily_limit,
                      "daily_baseline": self.settings.daily_baseline,
                      "local_minute_credits": minute_used, "local_daily_credits": day_used,
                      "daily_credits_remaining": daily_remaining if self._quota_confirmed(now) else None,
                      "minute_credits_remaining": (
                          self.settings.minute_limit - minute_used
                          if self._quota_confirmed(now) and minute_used is not None else None),
                      "local_policy_daily_headroom": daily_remaining,
                      "scope": "US allocation only; external/India use must be excluded from this allocation"},
            "estimate": {
                "interval_seconds": self.interval_seconds, "session_seconds": session,
                "remaining_session_seconds": remaining, "open_and_pending_symbols": len(required),
                "configured_position_slots": slots, "research_symbols": universe,
                "candidate_limit": candidates,
                "watched_symbols": watched, "watch_refreshes_per_day": self.settings.watch_refreshes_per_day,
                "credits_per_symbol": cost, "batch_credit_discount": False,
                "current_monitoring_credits_session": current_full,
                "configured_monitoring_credits_session": configured_full,
                "research_credits_per_cycle": research_per_cycle, "cycles_per_session": cycles,
                "incidental_credits_session": incidental, "safety_reserve_pct": self.settings.safety_reserve_pct,
                "required_daily_credits": estimated_day, "required_remaining_credits": estimated_remaining,
                "monitoring_credits_per_minute": minute_monitoring,
                "required_sustained_minute_credits": sustained_minute,
                "required_monitoring_and_entry_burst_credits": monitoring_and_entry_burst,
                "required_minute_credits": required_minute,
                "assumption": "Uncached forecast; 2 scanner reads/universe symbol and "
                              "2 research/signal reads/candidate/cycle, one selected strategy; "
                              "research is spread across the configured cycle interval; "
                              "bursts defer/reject at the credit limiter, not the exit lane; "
                              "all attempts charged, manual workloads remain hard-budgeted."},
            "protection_assured": False,
            "detail": "Read-only local evidence, not account certification or risk approval. "
                      "No automatic liquidation or stale-price fallback. Capacity loss blocks entries; "
                      "best-effort protective monitoring continues with validated quotes only."}

    def entry_blocker(self) -> str | None:
        blockers = self.status()["blockers"]
        return "US_MONITORING_CAPACITY: " + "; ".join(blockers) if blockers else None
