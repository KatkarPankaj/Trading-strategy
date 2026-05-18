from __future__ import annotations

import hashlib
import pickle
import time as _time_mod
from pathlib import Path

import pandas as pd

from .finnhub_client import fetch_candles, fetch_intraday_candles_df_params
from .nse_intraday import fetch_nse_intraday_ohlcv, symbol_market

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

_CACHE_DIR = Path(".cache") / "market_data"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CACHE_TTL_CURRENT_DAY = 300
_CACHE_TTL_HISTORICAL = 6 * 3600


def _cache_path(symbol: str, interval: str, period: str, backend: str) -> Path:
    key = f"{backend}_{symbol}_{interval}_{period}"
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return _CACHE_DIR / f"{h}.pkl"


def _cache_ttl(period: str) -> int:
    if period in ("1d", "2d"):
        return _CACHE_TTL_CURRENT_DAY
    return _CACHE_TTL_HISTORICAL


def _load_cache(symbol: str, interval: str, period: str, backend: str) -> pd.DataFrame | None:
    path = _cache_path(symbol, interval, period, backend)
    if not path.exists():
        return None
    ttl = _cache_ttl(period)
    if (_time_mod.time() - path.stat().st_mtime) > ttl:
        path.unlink(missing_ok=True)
        return None
    try:
        with path.open("rb") as f:
            return pickle.load(f)
    except Exception:
        path.unlink(missing_ok=True)
        return None


def _save_cache(symbol: str, interval: str, period: str, backend: str, df: pd.DataFrame) -> None:
    path = _cache_path(symbol, interval, period, backend)
    try:
        with path.open("wb") as f:
            pickle.dump(df, f)
    except Exception:
        pass


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def _normalize_intraday_frame(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    period: str,
    tz: str,
    *,
    market: str,
) -> pd.DataFrame:
    """Normalize OHLCV index to ``tz`` and clip to regular cash session for the routed market."""
    df = _flatten_columns(df.copy())
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns from provider: {missing}")

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()
    df = df.loc[:, REQUIRED_COLUMNS]
    df = df[~df.index.duplicated(keep="first")]

    if market == "us":
        df = df.tz_convert("America/New_York")
        df = df.between_time("09:30", "16:00")
    else:
        df = df.tz_convert("Asia/Kolkata")
        df = df.between_time("09:15", "15:30")

    if df.empty:
        raise ValueError(
            f"No rows in regular session for {symbol!r} (market={market})"
        )

    df = df.tz_convert(tz)
    _save_cache(symbol, interval, period, "fh" if market == "us" else "nse", df)
    return df


def _finnhub_candles_to_df(payload: dict) -> pd.DataFrame:
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        raise ValueError(f"Finnhub candle error: {payload!r}")
    ts = payload.get("t") or []
    if not ts:
        raise ValueError("Finnhub candles empty")
    o = payload.get("o") or []
    h = payload.get("h") or []
    low = payload.get("l") or []
    c = payload.get("c") or []
    v = payload.get("v") or []
    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True)
    df = pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "volume": v}, index=idx)
    for col in REQUIRED_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(how="all")


def _download_finnhub_intraday(
    symbol: str,
    interval: str,
    period: str,
    *,
    max_retries: int = 4,
    backoff_base: float = 5.0,
) -> pd.DataFrame:
    sym = str(symbol).upper().strip()
    res, from_u, to_u = fetch_intraday_candles_df_params(sym, interval, period)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            raw = fetch_candles(sym, resolution=res, from_unix=from_u, to_unix=to_u)
            df = _finnhub_candles_to_df(raw)
            if not df.empty:
                return df
            last_err = ValueError("empty Finnhub frame")
        except Exception as e:
            last_err = e
        if attempt < max_retries - 1:
            _time_mod.sleep(backoff_base * (2**attempt))
    raise ValueError(
        f"Finnhub intraday failed after {max_retries} attempts for {sym}: {last_err}"
    )


def fetch_intraday_data(
    symbol: str,
    interval: str,
    period: str,
    tz: str = "Asia/Kolkata",
    max_retries: int = 4,
    backoff_base: float = 5.0,
) -> pd.DataFrame:
    mkt = symbol_market(symbol)
    backend = "fh" if mkt == "us" else "nse"
    cached = _load_cache(symbol, interval, period, backend)
    if cached is not None:
        return cached

    if mkt == "us":
        df = _download_finnhub_intraday(
            symbol,
            interval,
            period,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
        return _normalize_intraday_frame(
            df, symbol, interval, period, tz, market="us"
        )

    df = fetch_nse_intraday_ohlcv(symbol, interval, period)
    return _normalize_intraday_frame(df, symbol, interval, period, tz, market="nse_equity")


def batch_fetch_intraday_data(
    symbols: list[str],
    interval: str,
    period: str,
    tz: str = "Asia/Kolkata",
    max_retries: int = 1,
    backoff_base: float = 1.0,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch intraday OHLCV per symbol (Finnhub US, NSE archives/index for India)."""
    results: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    if not symbols:
        return results, errors

    for sym in symbols:
        try:
            results[sym] = fetch_intraday_data(
                sym,
                interval,
                period,
                tz=tz,
                max_retries=max_retries,
                backoff_base=backoff_base,
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")
    return results, errors


def latest_bars(df: pd.DataFrame, count: int = 5) -> pd.DataFrame:
    return df.tail(count).copy()
