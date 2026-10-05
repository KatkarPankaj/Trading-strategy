"""Yahoo Finance adapter. Research and backtesting only: Yahoo is not a guaranteed execution-grade feed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from ..brokers import MarketStatus
from ..markets import MarketRegistry, default_markets
from ..models import Instrument
from .provider import (
    OHLCV,
    DataProviderError,
    DataUnavailable,
    MarketDataProvider,
    Quote,
    RateLimited,
    interval_delta,
)

SUFFIX = {"XNYS": "", "XNAS": "", "ARCX": "",
          "XNSE": ".NS", "XBOM": ".BO", "XETR": ".DE"}
Downloader = Callable[..., pd.DataFrame]


def yahoo_symbol(instrument: Instrument) -> str:
    try:
        return instrument.symbol + SUFFIX[instrument.exchange]
    except KeyError:
        raise DataUnavailable(
            f"no Yahoo symbol mapping for exchange {instrument.exchange}") from None


def _default_downloader(**kwargs: Any) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(progress=False, auto_adjust=False, threads=False, **kwargs)


class YahooProvider(MarketDataProvider):
    name = "yahoo"
    research_only = True

    def __init__(
        self,
        instruments: Mapping[str, Instrument],
        *,
        markets: MarketRegistry | None = None,
        downloader: Downloader = _default_downloader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._instruments = dict(instruments)
        self._markets = markets or default_markets()
        self._download = downloader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        for inst in self._instruments.values():
            if (inst.symbol == symbol or inst.instrument_id == symbol) and (market is None or inst.market == market.upper()):
                return inst
        raise DataUnavailable(f"unknown instrument {symbol!r}")

    def get_quote(self, instrument: Instrument) -> Quote:
        df = self._fetch(instrument, "1m", start=None, end=None, period="1d")
        if df.empty:
            raise DataUnavailable(
                f"no recent data for {instrument.instrument_id}")
        last = df.iloc[-1]
        return Quote(instrument.instrument_id, float(last["close"]), df.index[-1].to_pydatetime(), self.name,
                     volume=float(last["volume"]))

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        interval_delta(interval)
        return self._fetch(instrument, interval, start=start, end=end, period=None)

    def get_market_status(self, market: str) -> MarketStatus:
        now = self._clock()
        phase = self._markets.phase(market, now)
        return MarketStatus(market.upper(), phase.value == "REGULAR", now, phase.value)

    # ---- internals ----
    def _fetch(self, instrument: Instrument, interval: str, *, start: datetime | None,
               end: datetime | None, period: str | None) -> pd.DataFrame:
        kwargs: dict[str, Any] = {"tickers": yahoo_symbol(
            instrument), "interval": interval}
        if period:
            kwargs["period"] = period
        else:
            kwargs.update(start=start, end=end)
        try:
            raw = self._download(**kwargs)
        except DataProviderError:
            raise
        except Exception as exc:
            if "rate" in str(exc).lower() and "limit" in str(exc).lower():
                raise RateLimited(30.0) from exc
            raise ConnectionError(
                f"yahoo download failed: {type(exc).__name__}") from exc
        return self._normalize(raw, instrument)

    @staticmethod
    def _normalize(raw: pd.DataFrame, instrument: Instrument) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=list(OHLCV), index=pd.DatetimeIndex([], tz="UTC"))
        df = raw.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(
                c, tuple) else c for c in df.columns]
        df.columns = [str(c).lower() for c in df.columns]
        df = df[list(OHLCV)]
        idx = pd.DatetimeIndex(df.index)
        df.index = (idx.tz_localize(ZoneInfo(instrument.timezone))
                    if idx.tz is None else idx).tz_convert("UTC")
        return df[~df.index.duplicated(keep="last")].sort_index()
