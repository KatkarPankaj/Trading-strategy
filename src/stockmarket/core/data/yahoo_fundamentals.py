"""Timestamped, research-only company earnings observations from Yahoo Finance."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

from ..models import Instrument
from ..research import ResearchObservation
from .yahoo import yahoo_symbol

TickerFactory = Callable[[str], Any]


def _default_ticker_factory(symbol: str) -> Any:
    import yfinance as yf

    return yf.Ticker(symbol)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


class YahooEarningsObservationProvider:
    """Expose reported EPS events; do not treat undated Yahoo profile fields as fresh."""

    name = "yahoo_finance_earnings"

    def __init__(self, ticker_factory: TickerFactory = _default_ticker_factory) -> None:
        self._ticker_factory = ticker_factory

    def get_observation(
        self,
        instrument: Instrument,
        *,
        component: Literal["sector", "fundamental"],
        as_of: datetime,
        max_age: timedelta,
    ) -> ResearchObservation | None:
        if component != "fundamental":
            return None
        if not isinstance(as_of, datetime) or as_of.tzinfo is None \
                or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")

        symbol = yahoo_symbol(instrument)
        earnings = self._ticker_factory(symbol).get_earnings_dates(limit=100)
        if not isinstance(earnings, pd.DataFrame):
            raise TypeError("Yahoo earnings response must be a DataFrame")
        if earnings.empty or "Reported EPS" not in earnings.columns:
            return None

        zone = ZoneInfo(instrument.timezone)
        as_of_utc = as_of.astimezone(timezone.utc)
        candidates: list[tuple[datetime, float, float | None, float | None]] = []
        for raw_time, row in earnings.iterrows():
            timestamp = pd.Timestamp(raw_time)
            if pd.isna(timestamp):
                continue
            event_at = timestamp.to_pydatetime()
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=zone)
            local_event = event_at.astimezone(zone)
            if local_event.time().replace(tzinfo=None) == time(0, 0):
                local_event = local_event.replace(
                    hour=23, minute=59, second=59, microsecond=0)
            event_at = local_event.astimezone(timezone.utc)
            if event_at > as_of_utc:
                continue
            reported = _number(row.get("Reported EPS"))
            if reported is None:
                continue
            estimate = _number(row.get("EPS Estimate"))
            surprise = _number(row.get("Surprise(%)"))
            if surprise is None and estimate not in (None, 0.0):
                surprise = 100.0 * (reported - estimate) / abs(estimate)
            candidates.append((event_at, reported, estimate, surprise))

        if not candidates:
            return None
        observed_at, reported, estimate, surprise = max(
            candidates, key=lambda item: item[0])
        if as_of_utc - observed_at > max_age:
            return None

        facts = [
            f"Reported EPS: {reported:g}",
            f"EPS estimate: {estimate:g}" if estimate is not None
            else "EPS estimate: unavailable",
            f"EPS surprise percent: {surprise:g}" if surprise is not None
            else "EPS surprise percent: unavailable",
            f"Report event timestamp: {observed_at.isoformat()}",
            "Source fields are Yahoo Finance earnings-calendar values.",
        ]
        return ResearchObservation(
            instrument_id=instrument.instrument_id,
            market=instrument.market,
            component="fundamental",
            subject="Most recent reported earnings",
            content="; ".join(facts),
            observed_at=observed_at,
            source=self.name,
            reference=f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/",
        )
