"""Shared canonical instrument discovery and read-only market observations."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .data.provider import DataProviderError, Quote, expected_provider_name
from .data.quality import validate_quote
from .markets import MarketRegistry
from .models import Instrument


class MarketWatch:
    def __init__(
        self, instruments: Mapping[str, Instrument], markets: MarketRegistry,
        repository: Any, providers: Mapping[str, Any], *,
        max_age: Mapping[str, timedelta],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.instruments = instruments
        self.markets = markets
        self.repository = repository
        self.providers = dict(providers)
        self.max_age = dict(max_age)
        if set(self.providers) != set(self.max_age) or any(
                age <= timedelta(0) for age in self.max_age.values()):
            raise ValueError("each watch market requires a positive freshness policy")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()
        self._flights = {code: Lock() for code in providers}
        self._quotes: dict[str, tuple[Quote | None, datetime, str | None]] = {}

    def _instrument(self, identity: str) -> Instrument:
        instrument = self.instruments.get(identity)
        if instrument is None or instrument.market not in self.providers:
            raise ValueError("unknown or disabled canonical instrument")
        return instrument

    def profiles(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [{
            "market": code, "currency": self.markets.get(code).currency,
            "timezone": self.markets.get(code).timezone,
            "phase": self.markets.phase(code, now).value,
            "calendar_covered": self.markets.get(code).is_covered(
                now.astimezone(ZoneInfo(self.markets.get(code).timezone)).date()),
            "instrument_count": sum(i.market == code for i in self.instruments.values()),
            "provider": self.providers[code].name,
            "data_origin": "SYNTHETIC" if self.providers[code].name == "mock" else "UNATTESTED",
            "max_quote_age_seconds": self.max_age[code].total_seconds(),
            "execution": "NOT_SUBMITTED",
        } for code in sorted(self.providers)]

    def search(self, market: str, query: str = "", *, after: str = "",
               limit: int = 50) -> dict[str, Any]:
        if market not in self.providers or not 1 <= limit <= 100:
            raise ValueError("invalid market or search limit")
        needle = query.strip().casefold()
        found = sorted((
            i for i in self.instruments.values()
            if i.market == market and i.instrument_id > after
            and (not needle or needle in f"{i.instrument_id} {i.name or ''}".casefold())
        ), key=lambda i: i.instrument_id)
        rows = found[:limit]
        return {"items": [self.describe(i) for i in rows],
                "next_cursor": rows[-1].instrument_id if len(found) > limit else None}

    @staticmethod
    def describe(instrument: Instrument) -> dict[str, Any]:
        return {
            "instrument_id": instrument.instrument_id, "symbol": instrument.symbol,
            "name": instrument.name, "market": instrument.market,
            "exchange": instrument.exchange, "currency": instrument.currency,
            "timezone": instrument.timezone, "tick_size": instrument.tick_size,
            "lot_size": instrument.lot_size, "asset_class": instrument.asset_class.value,
            "active": instrument.active, "tradable": instrument.tradable,
        }

    def add(self, identity: str) -> None:
        self._instrument(identity)
        self.repository.add(identity, self.clock())

    def remove(self, identity: str) -> None:
        self._instrument(identity)
        self.repository.remove(identity)

    def rows(self, market: str) -> list[dict[str, Any]]:
        if market not in self.providers:
            raise ValueError("unknown or disabled market")
        members = self.repository.list()
        if len(members) > 100:
            raise ValueError("watchlist exceeds the supported limit")
        observations = []
        for row in members:
            instrument = self.instruments.get(row["instrument_id"])
            if instrument is None:
                raise ValueError("watchlist references missing canonical metadata")
            if instrument.market == market:
                observations.append(self.observation(instrument.instrument_id))
        return observations

    def observation(self, identity: str) -> dict[str, Any]:
        instrument = self._instrument(identity)
        now = self.clock()
        with self._lock:
            cached = self._quotes.get(identity)
        quote, fetched_at, error = cached if cached else (None, None, None)
        status = "NOT_FETCHED"
        age = None
        if error:
            status = error
        elif quote is not None:
            report = validate_quote(quote, now, self.max_age[instrument.market])
            status = "FRESH" if report.ok else (
                "STALE" if "STALE_QUOTE" in report.issues else "INVALID")
            age = (now - quote.timestamp).total_seconds()
        return {
            **self.describe(instrument), "quote_status": status,
            "price": quote.price if quote is not None and status in {"FRESH", "STALE"} else None,
            "quote_timestamp": quote.timestamp if quote is not None else None,
            "fetched_at": fetched_at, "age_seconds": age,
            "provider": quote.provider if quote else self.providers[instrument.market].name,
            "data_origin": "SYNTHETIC" if self.providers[instrument.market].name == "mock" else "UNATTESTED",
            "phase": self.markets.get(instrument.market).phase(now, instrument.exchange).value,
            "eligible_for_order": False,
            "execution": "NOT_SUBMITTED",
        }

    def refresh(self, market: str) -> dict[str, Any]:
        if market not in self.providers:
            raise ValueError("unknown or disabled market")
        if not self._flights[market].acquire(blocking=False):
            raise ValueError("quote refresh already in progress for this market")
        try:
            rows = self.rows(market)
            if len(rows) > 10:
                raise ValueError("quote refresh supports at most 10 watched instruments per market")
            started = time.monotonic()
            checked = 0
            for row in rows:
                if time.monotonic() - started >= 5:
                    break
                instrument = self._instrument(row["instrument_id"])
                quote = None
                error = None
                try:
                    candidate = self.providers[market].get_quote(instrument)
                    # Age is evaluated at display time; retain stale, otherwise valid observations.
                    report = validate_quote(candidate, self.clock(), timedelta(days=36500))
                    if not report.ok or candidate.instrument_id != instrument.instrument_id \
                            or candidate.provider != expected_provider_name(
                                self.providers[market], instrument) \
                            or not instrument.is_valid_price(candidate.price):
                        error = "INVALID"
                    else:
                        quote = candidate
                except DataProviderError as exc:
                    error = exc.code
                with self._lock:
                    self._quotes[instrument.instrument_id] = (quote, self.clock(), error)
                checked += 1
            return {"items": self.rows(market), "checked": checked,
                    "incomplete": checked != len(rows), "execution": "NOT_SUBMITTED"}
        finally:
            self._flights[market].release()

    def close(self) -> None:
        for provider in self.providers.values():
            provider.close()
