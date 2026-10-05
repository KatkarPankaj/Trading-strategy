"""Provider-independent market data contract. Strategies and services depend on this, never on a vendor."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

import pandas as pd

from ..brokers import MarketStatus
from ..models import Instrument

INTERVALS: Mapping[str, timedelta] = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30), "1h": timedelta(hours=1), "1d": timedelta(days=1),
}
OHLCV = ("open", "high", "low", "close", "volume")


class DataProviderError(Exception):
    pass


class DataUnavailable(DataProviderError):
    """No usable data (outage, unknown instrument, open circuit). Callers must not trade."""


class DataQualityError(DataProviderError):
    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__("data failed validation: " + ", ".join(issues))


class RateLimited(DataProviderError):
    def __init__(self, retry_after: float = 1.0) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limited; retry after {retry_after}s")


class ProviderTimeout(TimeoutError, DataProviderError):
    pass


@dataclass(frozen=True, slots=True)
class Quote:
    instrument_id: str
    price: float
    timestamp: datetime
    provider: str
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None


def interval_delta(interval: str) -> timedelta:
    try:
        return INTERVALS[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; use one of {sorted(INTERVALS)}") from None


class MarketDataProvider(ABC):
    """Raw access to a data source. Quality checks, retries and timeouts live in ResilientProvider."""

    name: str
    research_only: bool = False  # True for sources that must never drive live execution

    @abstractmethod
    def get_instrument(self, symbol: str, market: str |
                       None = None) -> Instrument: ...

    @abstractmethod
    def get_quote(self, instrument: Instrument) -> Quote: ...

    @abstractmethod
    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Bars with a sorted, timezone-aware (UTC) index and open/high/low/close/volume columns."""

    @abstractmethod
    def get_market_status(self, market: str) -> MarketStatus: ...

    def get_intraday_bars(self, instrument: Instrument, interval: str, lookback: timedelta,
                          now: datetime) -> pd.DataFrame:
        return self.get_ohlcv(instrument, interval, now - lookback, now)

    def get_historical_data(self, instrument: Instrument, start: datetime, end: datetime,
                            interval: str = "1d") -> pd.DataFrame:
        return self.get_ohlcv(instrument, interval, start, end)
