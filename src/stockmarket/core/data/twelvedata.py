"""Twelve Data REST adapter for research data (US, India and Germany via MIC codes).

Research-only in this platform: its suitability as an execution-grade feed has not been
validated, and real-time/exchange coverage depends on the account's Twelve Data plan.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from ..brokers import MarketStatus
from ..markets import MarketRegistry, default_markets
from ..models import Instrument
from .provider import (
    OHLCV,
    DataUnavailable,
    MarketDataProvider,
    ProviderTimeout,
    Quote,
    RateLimited,
    interval_delta,
)

_ENDPOINT = "https://api.twelvedata.com/time_series"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_OUTPUTSIZE = 5000
INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min",
             "30m": "30min", "1h": "1h", "1d": "1day"}
SUPPORTED_MICS = frozenset({"XNYS", "XNAS", "ARCX", "XNSE", "XBOM", "XETR"})
# Twelve Data rejects the operating MIC XNAS; it lists NASDAQ securities under
# their market-segment MICs, so NASDAQ is requested by exchange name instead.
_EXCHANGE_PARAM = {"XNAS": "NASDAQ"}
_RESPONSE_MIC_ALIASES = {"XNAS": frozenset({"XNAS", "XNGS", "XNMS", "XNCM"})}
Transport = Callable[[Mapping[str, str], float], Any]


class _TwelveDataHTTPError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"HTTP {code}")


def _default_transport(params: Mapping[str, str], timeout: float) -> Any:
    request = Request(f"{_ENDPOINT}?{urlencode(params)}", headers={
        "Accept": "application/json", "User-Agent": "Trading-strategy-research/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS endpoint
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        # Drop the original exception: its URL carries the API key.
        message = ""
        try:
            body = json.loads(exc.read(64 * 1024).decode("utf-8"))
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                message = body["message"]
        except (OSError, ValueError):
            pass
        raise _TwelveDataHTTPError(exc.code, message) from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise DataUnavailable("Twelve Data response exceeded the size limit")
    return json.loads(raw.decode("utf-8"))


class TwelveDataProvider(MarketDataProvider):
    name = "twelvedata"
    research_only = True

    def __init__(
        self,
        instruments: Mapping[str, Instrument],
        api_key: str,
        *,
        markets: MarketRegistry | None = None,
        transport: Transport = _default_transport,
        timeout: float = 15.0,
        requests_per_minute: int = 8,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Twelve Data API key must be a non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 0 < timeout <= 60:
            raise ValueError("timeout must be greater than 0 and at most 60 seconds")
        if isinstance(requests_per_minute, bool) or not isinstance(requests_per_minute, int) \
                or not 1 <= requests_per_minute <= 6000:
            raise ValueError("requests_per_minute must be an integer from 1 to 6000")
        self._instruments = dict(instruments)
        self._api_key = api_key.strip()
        self._markets = markets or default_markets()
        self._transport = transport
        self._timeout = float(timeout)
        self._rpm = requests_per_minute
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TwelveDataProvider(instruments={len(self._instruments)}, api_key=***)"

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        for inst in self._instruments.values():
            if (inst.symbol == symbol or inst.instrument_id == symbol) and (
                    market is None or inst.market == market.upper()):
                return inst
        raise DataUnavailable(f"unknown instrument {symbol!r}")

    def get_quote(self, instrument: Instrument) -> Quote:
        df = self._fetch(instrument, {"interval": "1min", "outputsize": "5"})
        if df.empty:
            raise DataUnavailable(f"no recent data for {instrument.instrument_id}")
        latest = df.index.max()
        last = df.loc[[latest]].iloc[-1]
        return Quote(instrument.instrument_id, float(last["close"]), latest.to_pydatetime(),
                     self.name, volume=float(last["volume"]))

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime,
                  end: datetime) -> pd.DataFrame:
        step = interval_delta(interval)
        if interval not in INTERVALS:
            raise ValueError(f"Twelve Data does not support interval {interval!r}")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")
        expected = int((end - start) / step) + 1
        return self._fetch(instrument, {
            "interval": INTERVALS[interval],
            "start_date": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "outputsize": str(min(_MAX_OUTPUTSIZE, max(1, expected))),
        })

    def get_market_status(self, market: str) -> MarketStatus:
        now = self._clock()
        phase = self._markets.phase(market, now)
        return MarketStatus(market.upper(), phase.value == "REGULAR", now, phase.value)

    # ---- internals ----
    def _throttle(self) -> None:
        with self._lock:
            now = self._monotonic()
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            if len(self._sent) >= self._rpm:
                self._sleep(max(0.0, 60.0 - (now - self._sent[0])))
                now = self._monotonic()
                self._sent.popleft()
            self._sent.append(now)

    def _fetch(self, instrument: Instrument, extra: Mapping[str, str]) -> pd.DataFrame:
        if instrument.exchange not in SUPPORTED_MICS:
            raise DataUnavailable(
                f"no Twelve Data mapping for exchange {instrument.exchange}")
        venue = ({"exchange": _EXCHANGE_PARAM[instrument.exchange]}
                 if instrument.exchange in _EXCHANGE_PARAM
                 else {"mic_code": instrument.exchange})
        params = {
            "symbol": instrument.symbol, **venue,
            "timezone": "UTC", "order": "asc", "format": "JSON", **extra,
            "apikey": self._api_key,
        }
        self._throttle()
        try:
            payload = self._transport(params, self._timeout)
        except _TwelveDataHTTPError as exc:
            self._raise_for_code(exc.code, exc.message)
        except TimeoutError:
            raise ProviderTimeout("Twelve Data request timed out") from None
        except (URLError, OSError) as exc:
            raise ConnectionError(
                f"Twelve Data request failed: {type(exc).__name__}") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataUnavailable("Twelve Data returned a non-JSON response") from None
        if not isinstance(payload, dict):
            raise DataUnavailable("Twelve Data response must be a JSON object")
        if payload.get("status") == "error":
            code = payload.get("code")
            message = payload.get("message")
            self._raise_for_code(code if isinstance(code, int) else 0,
                                 message if isinstance(message, str) else "")
        return self._normalize(payload, instrument)

    @staticmethod
    def _raise_for_code(code: int, message: str = "") -> None:
        if code == 429:
            raise RateLimited(60.0)
        lowered = message.lower()
        if "plan" in lowered and ("upgrad" in lowered or "available starting" in lowered):
            raise DataUnavailable(
                "Twelve Data plan does not include this exchange/symbol (paid plan required)")
        if code in (401, 403):
            raise DataUnavailable(
                "Twelve Data rejected the API key or the plan lacks this exchange/data")
        if code == 400 or code == 404:
            raise DataUnavailable("Twelve Data has no data for this symbol/MIC/range")
        if code >= 500:
            raise ConnectionError(f"Twelve Data server error {code}")
        raise DataUnavailable(f"Twelve Data request failed with code {code}")

    @staticmethod
    def _normalize(payload: Mapping[str, Any], instrument: Instrument) -> pd.DataFrame:
        meta = payload.get("meta")
        if isinstance(meta, dict):
            symbol, mic = meta.get("symbol"), meta.get("mic_code")
            accepted = _RESPONSE_MIC_ALIASES.get(
                instrument.exchange, frozenset({instrument.exchange}))
            if (symbol is not None and str(symbol).upper() != instrument.symbol.upper()) or (
                    mic is not None and str(mic).upper() not in accepted):
                raise DataUnavailable("Twelve Data returned a different instrument")
        values = payload.get("values")
        if values is None:
            values = []
        if not isinstance(values, list):
            raise DataUnavailable("Twelve Data values must be a list")
        index: list[pd.Timestamp] = []
        rows: list[dict[str, float]] = []
        for row in values:
            if not isinstance(row, dict) or not isinstance(row.get("datetime"), str):
                raise DataUnavailable("Twelve Data returned an invalid bar")
            try:
                stamp = pd.Timestamp(row["datetime"])
                stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
                # Missing volume stays NaN so data validation can reject it.
                rows.append({name: float(row[name]) if row.get(name) not in (None, "")
                             else float("nan") for name in OHLCV})
            except (TypeError, ValueError):
                raise DataUnavailable("Twelve Data returned a non-numeric bar") from None
            index.append(stamp)
        return pd.DataFrame(rows, columns=list(OHLCV),
                            index=pd.DatetimeIndex(index, tz="UTC"))
