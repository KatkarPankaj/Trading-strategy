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
    """Base provider failure. `code` is a stable machine-readable reason for callers and audits."""

    code = "DATA_PROVIDER_ERROR"


class DataUnavailable(DataProviderError):
    """No usable data (outage, unknown instrument, open circuit). Callers must not trade."""

    code = "DATA_UNAVAILABLE"


class ProviderAuthenticationFailed(DataUnavailable):
    """The provider rejected the configured credentials."""

    code = "AUTHENTICATION_FAILED"


class EntitlementMissing(DataUnavailable):
    """The account/plan is not entitled to this exchange, symbol, interval or depth."""

    code = "ENTITLEMENT_MISSING"


class UnsupportedSymbol(DataUnavailable):
    """The provider does not know the requested symbol on the requested venue."""

    code = "UNSUPPORTED_SYMBOL"


class UnsupportedMarket(DataUnavailable):
    """The provider (or its configured route) does not serve this market or exchange."""

    code = "UNSUPPORTED_MARKET"


class UnsupportedInterval(DataUnavailable, ValueError):
    """The provider does not supply this bar interval (also a ValueError for older callers)."""

    code = "UNSUPPORTED_INTERVAL"


class HistoryDepthUnavailable(DataUnavailable):
    """The requested history lies beyond what the provider can serve for that interval."""

    code = "HISTORY_DEPTH_UNAVAILABLE"


class DataQualityError(DataProviderError):
    code = "DATA_QUALITY_FAILED"

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        if any(issue.startswith("STALE_") for issue in issues):
            self.code = "STALE_DATA"
        super().__init__("data failed validation: " + ", ".join(issues))


class RateLimited(DataProviderError):
    code = "RATE_LIMITED"

    def __init__(self, retry_after: float = 1.0) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limited; retry after {retry_after}s")


class ProviderTimeout(TimeoutError, DataProviderError):
    code = "PROVIDER_TIMEOUT"


def provider_error_code(error: BaseException) -> str:
    """Stable reason code for any provider failure; unknown failures stay distinguishable."""
    if isinstance(error, DataProviderError):
        return error.code
    if isinstance(error, TimeoutError):
        return "PROVIDER_TIMEOUT"
    if isinstance(error, ConnectionError):
        return "PROVIDER_CONNECTION_FAILED"
    return "PROVIDER_ERROR"


def expected_provider_name(provider: object, instrument: Instrument) -> str:
    """The provenance name a quote for ``instrument`` must carry; per-market routers override it."""
    resolve = getattr(provider, "provider_name_for", None)
    return resolve(instrument) if callable(resolve) else getattr(provider, "name")


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
