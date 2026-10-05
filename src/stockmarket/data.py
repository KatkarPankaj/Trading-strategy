from __future__ import annotations

import time as _time_mod
import hashlib
import pickle
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import yfinance as yf

from .config import TradingConfig
from .core.market_session import MarketSession


REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# Cache directory for downloaded data
_CACHE_DIR = Path(".cache") / "market_data"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# How long intraday cache stays valid (seconds).
# Current-day bars expire after 5 min; older periods stay for 6 hours.
_CACHE_TTL_CURRENT_DAY = 300       # 5 minutes
_CACHE_TTL_HISTORICAL = 6 * 3600  # 6 hours


def _cache_path(symbol: str, interval: str, period: str) -> Path:
    key = f"{symbol}_{interval}_{period}"
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return _CACHE_DIR / f"{h}.pkl"


def _cache_ttl(period: str) -> int:
    """Use short TTL for today-only periods, longer for historical."""
    if period in ("1d", "2d"):
        return _CACHE_TTL_CURRENT_DAY
    return _CACHE_TTL_HISTORICAL


def _load_cache(symbol: str, interval: str, period: str) -> pd.DataFrame | None:
    path = _cache_path(symbol, interval, period)
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


def _save_cache(symbol: str, interval: str, period: str, df: pd.DataFrame) -> None:
    path = _cache_path(symbol, interval, period)
    try:
        with path.open("wb") as f:
            pickle.dump(df, f)
    except Exception:
        pass


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def _download_yahoo(symbol: str, interval: str, period: str,
                    max_retries: int = 4, backoff_base: float = 5.0) -> pd.DataFrame:
    """Download from Yahoo Finance with exponential backoff retries."""
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            df = yf.download(
                tickers=symbol,
                interval=interval,
                period=period,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if not df.empty:
                return df
            last_err = ValueError(f"Empty response on attempt {attempt + 1}")
        except Exception as e:
            last_err = e

        wait = backoff_base * (2 ** attempt)
        _time_mod.sleep(wait)

    raise ValueError(
        f"Yahoo Finance failed after {max_retries} attempts for {symbol}: {last_err}"
    )


def fetch_intraday_data(
    symbol: str,
    interval: str,
    period: str,
    tz: str | None = None,
    session: MarketSession | None = None,
    max_retries: int = 4,
    backoff_base: float = 5.0,
) -> pd.DataFrame:
    active_session = session or MarketSession.from_config(
        TradingConfig(market_timezone=tz or TradingConfig().market_timezone)
    )
    # 1. Check disk cache first
    cached = _load_cache(symbol, interval, period)
    if cached is not None:
        return _filter_to_session(cached, active_session)

    # 2. Download with retry
    df = _download_yahoo(
        symbol,
        interval,
        period,
        max_retries=max_retries,
        backoff_base=backoff_base,
    )

    df = _flatten_columns(df)
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns from provider: {missing}")

    if df.index.tz is None:
        df.index = df.index.tz_localize(
            "UTC").tz_convert(active_session.timezone)
    else:
        df.index = df.index.tz_convert(active_session.timezone)

    df = df.sort_index()
    df = df.loc[:, REQUIRED_COLUMNS]
    df = df[~df.index.duplicated(keep="first")]

    df = _filter_to_session(df, active_session)

    # 3. Save to cache before returning
    _save_cache(symbol, interval, period, df)

    return df


def _filter_to_session(
    df: pd.DataFrame,
    session: MarketSession,
) -> pd.DataFrame:
    normalized = df.copy()
    if normalized.index.tz is None:
        normalized.index = normalized.index.tz_localize("UTC")
    normalized.index = normalized.index.tz_convert(session.timezone)
    normalized = normalized.between_time(
        session.market_open,
        session.market_close,
        inclusive="both",
    )
    if normalized.empty:
        raise ValueError(
            "Data exists but no rows are within the configured market session")
    return normalized


def latest_bars(df: pd.DataFrame, count: int = 5) -> pd.DataFrame:
    return df.tail(count).copy()
