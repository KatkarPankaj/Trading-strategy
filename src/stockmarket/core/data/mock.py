"""Deterministic in-memory provider for paper trading, tests and demos. Never touches the network."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping

import pandas as pd

from ..brokers import MarketStatus
from ..markets import MarketRegistry, default_markets
from ..models import Instrument
from .provider import DataUnavailable, MarketDataProvider, Quote, interval_delta


class MockProvider(MarketDataProvider):
    name = "mock"
    research_only = True  # not a real feed: rejected for live trading by configuration

    def __init__(
        self,
        instruments: Mapping[str, Instrument],
        bars: Mapping[str, pd.DataFrame] | None = None,
        *,
        markets: MarketRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._instruments = dict(instruments)
        self._bars = dict(bars or {})
        self._quotes: dict[str, Quote] = {}
        self._markets = markets or default_markets()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def set_quote(self, instrument_id: str, price: float, timestamp: datetime | None = None, **extra: float) -> None:
        self._quotes[instrument_id] = Quote(
            instrument_id, price, timestamp or self._clock(), self.name, **extra)

    def set_bars(self, instrument_id: str, df: pd.DataFrame) -> None:
        self._bars[instrument_id] = df

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        for inst in self._instruments.values():
            if (inst.symbol == symbol or inst.instrument_id == symbol) and (market is None or inst.market == market.upper()):
                return inst
        raise DataUnavailable(f"unknown instrument {symbol!r}")

    def get_quote(self, instrument: Instrument) -> Quote:
        try:
            return self._quotes[instrument.instrument_id]
        except KeyError:
            raise DataUnavailable(
                f"no quote for {instrument.instrument_id}") from None

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        interval_delta(interval)
        df = self._bars.get(instrument.instrument_id)
        if df is None:
            raise DataUnavailable(f"no bars for {instrument.instrument_id}")
        # a bar can never be served before it exists
        cutoff = min(end, self._clock())
        return df.loc[(df.index >= start) & (df.index <= cutoff)].copy()

    def get_market_status(self, market: str) -> MarketStatus:
        now = self._clock()
        phase = self._markets.phase(market, now)
        return MarketStatus(market.upper(), phase.value == "REGULAR", now, phase.value)
