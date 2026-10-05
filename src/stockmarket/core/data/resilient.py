"""Reliability wrapper: timeouts, retries with backoff, rate-limit handling, circuit breaker, and quality gates."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import pandas as pd

from ..brokers import MarketStatus
from ..models import Instrument
from ..resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, call_with_retry
from .provider import (
    DataProviderError,
    DataQualityError,
    DataUnavailable,
    MarketDataProvider,
    ProviderTimeout,
    Quote,
    RateLimited,
)
from .quality import validate_bars, validate_quote


@dataclass(frozen=True, slots=True)
class _Rejected:
    error: DataProviderError


@dataclass(frozen=True, slots=True)
class DataPolicy:
    timeout_seconds: float = 10.0
    max_quote_age: timedelta = timedelta(seconds=60)
    max_bar_staleness: timedelta = timedelta(minutes=5)
    max_rate_limit_wait: float = 60.0
    retry: RetryPolicy = RetryPolicy()


class ResilientProvider:
    """What the rest of the platform uses. Returns only data that passed validation, otherwise raises."""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        policy: DataPolicy = DataPolicy(),
        breaker: CircuitBreaker | None = None,
        health: Any = None,
        logger: Any = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._p, self._policy = provider, policy
        self._breaker = breaker or CircuitBreaker()
        self._health, self._log = health, logger
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="market-data")

    @property
    def name(self) -> str:
        return self._p.name

    @property
    def research_only(self) -> bool:
        return self._p.research_only

    @property
    def breaker_state(self) -> str:
        return self._breaker.state

    # ---- validated operations ----
    def get_quote(self, instrument: Instrument) -> Quote:
        quote = self._call(lambda: self._p.get_quote(instrument))
        report = validate_quote(quote, self._clock(),
                                self._policy.max_quote_age)
        if not report.ok:
            self._warn("quote rejected", instrument, report.issues)
            raise DataQualityError(report.issues)
        if self._health is not None:
            self._health.record_market_data(quote.timestamp)
        return quote

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime, end: datetime,
                  *, require_fresh: bool = False) -> pd.DataFrame:
        df = self._call(lambda: self._p.get_ohlcv(
            instrument, interval, start, end))
        now = self._clock()
        report = validate_bars(df, interval, timezone=instrument.timezone,
                               now=now if require_fresh else None,
                               max_age=self._policy.max_bar_staleness if require_fresh else None)
        if not report.ok:
            self._warn("bars rejected", instrument, report.issues)
            raise DataQualityError(report.issues)
        if require_fresh and self._health is not None:
            self._health.record_market_data(df.index.max().to_pydatetime())
        return df

    def get_intraday_bars(self, instrument: Instrument, interval: str, lookback: timedelta) -> pd.DataFrame:
        now = self._clock()
        return self.get_ohlcv(instrument, interval, now - lookback, now, require_fresh=True)

    def get_historical_data(self, instrument: Instrument, start: datetime, end: datetime,
                            interval: str = "1d") -> pd.DataFrame:
        return self.get_ohlcv(instrument, interval, start, end)

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        return self._call(lambda: self._p.get_instrument(symbol, market))

    def get_market_status(self, market: str) -> MarketStatus:
        return self._call(lambda: self._p.get_market_status(market))

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- internals ----
    def _call(self, fn: Callable[[], Any]) -> Any:
        def guarded() -> Any:
            try:
                return self._with_retries(fn)
            except TimeoutError:
                raise  # outages count against the breaker
            except DataProviderError as exc:
                # a definitive answer (e.g. unknown instrument) is not an outage
                return _Rejected(exc)

        try:
            result = self._breaker.call(guarded)
        except CircuitOpenError as exc:
            raise DataUnavailable("data provider circuit is open") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise DataUnavailable(
                f"data provider failed after retries: {type(exc).__name__}") from exc
        except DataProviderError:
            raise
        except Exception as exc:  # a provider bug or malformed payload must never reach a trading decision
            raise DataUnavailable(
                f"data provider error: {type(exc).__name__}") from exc
        if isinstance(result, _Rejected):
            raise result.error
        return result

    def _with_retries(self, fn: Callable[[], Any]) -> Any:
        def attempt() -> Any:
            try:
                return self._run_with_timeout(fn)
            except RateLimited as exc:
                self._sleep(
                    min(exc.retry_after, self._policy.max_rate_limit_wait))
                # retried by the policy below
                raise ConnectionError("rate limited") from exc

        return call_with_retry(attempt, self._policy.retry, retry_on=(ConnectionError, TimeoutError),
                               sleep=self._sleep)

    def _run_with_timeout(self, fn: Callable[[], Any]) -> Any:
        future = self._pool.submit(fn)
        try:
            return future.result(timeout=self._policy.timeout_seconds)
        except FutureTimeout as exc:
            future.cancel()
            raise ProviderTimeout(
                f"no answer within {self._policy.timeout_seconds}s") from exc

    def _warn(self, message: str, instrument: Instrument, issues: tuple[str, ...]) -> None:
        if self._log is not None:
            self._log.warning(f"market data {message}", symbol=instrument.symbol, issues=list(issues),
                              provider=self._p.name)


def quote_source(
    provider: ResilientProvider,
    instruments: Mapping[str, Instrument],
) -> Callable[[str], tuple[float, datetime] | None]:
    """Adapter for TradingService: any data problem becomes None, which the risk engine rejects as invalid data."""

    def source(instrument_id: str) -> tuple[float, datetime] | None:
        instrument = instruments.get(instrument_id)
        if instrument is None:
            return None
        try:
            quote = provider.get_quote(instrument)
        except DataProviderError:
            return None
        return quote.price, quote.timestamp

    return source
