from __future__ import annotations

import time as _time_mod
import hashlib
import pickle
from datetime import datetime, date
from pathlib import Path

import pandas as pd

from . import yfinance_tz  # noqa: F401  # configure cache path before yfinance
import yfinance as yf


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


def _normalize_intraday_frame(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    period: str,
    tz: str,
) -> pd.DataFrame:
    """Shared normalization after downloading Yahoo OHLCV for one symbol."""
    df = _flatten_columns(df.copy())
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns from provider: {missing}")

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(tz)
    else:
        df.index = df.index.tz_convert(tz)

    df = df.sort_index()
    df = df.loc[:, REQUIRED_COLUMNS]
    df = df[~df.index.duplicated(keep="first")]
    df = df.between_time("09:15", "15:30")

    if df.empty:
        raise ValueError(
            "Data exists but no rows are in regular market session 09:15-15:30 IST"
        )

    _save_cache(symbol, interval, period, df)
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
    tz: str = "Asia/Kolkata",
    max_retries: int = 4,
    backoff_base: float = 5.0,
) -> pd.DataFrame:
    # 1. Check disk cache first
    cached = _load_cache(symbol, interval, period)
    if cached is not None:
        return cached

    # 2. Download with retry
    df = _download_yahoo(
        symbol,
        interval,
        period,
        max_retries=max_retries,
        backoff_base=backoff_base,
    )

    df = _normalize_intraday_frame(df, symbol, interval, period, tz)

    return df


def batch_fetch_intraday_data(
    symbols: list[str],
    interval: str,
    period: str,
    tz: str = "Asia/Kolkata",
    max_retries: int = 1,
    backoff_base: float = 1.0,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch many NSE/BSE Yahoo symbols with one ``yf.download`` where possible.

    Returns (ok_map, error_messages).
    """
    results: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    if not symbols:
        return results, errors

    need: list[str] = []
    for sym in symbols:
        cached = _load_cache(sym, interval, period)
        if cached is not None:
            results[sym] = cached
        else:
            need.append(sym)

    if not need:
        return results, errors

    tickers = " ".join(need)
    last_err: Exception | None = None
    raw: pd.DataFrame | None = None
    for attempt in range(max(1, max_retries)):
        try:
            raw = yf.download(
                tickers=tickers,
                interval=interval,
                period=period,
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
            )
            if raw is not None and not raw.empty:
                break
            last_err = ValueError("empty batch response")
        except Exception as e:
            last_err = e
        wait = backoff_base * (2 ** attempt)
        _time_mod.sleep(wait)

    if raw is None or raw.empty:
        errors.append(f"batch download failed: {last_err}")
        return results, errors

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = list(raw.columns.get_level_values(0).unique())
        for s in need:
            key = None
            for cand in level0:
                if str(cand).upper() == str(s).upper():
                    key = cand
                    break
            if key is None:
                errors.append(f"{s}: not present in batch Yahoo response")
                continue
            try:
                sub = raw[key].copy()
                sub.columns = [str(c).strip() for c in sub.columns]
                results[s] = _normalize_intraday_frame(
                    sub, s, interval, period, tz
                )
            except Exception as e:
                errors.append(f"{s}: {e}")
    else:
        if len(need) == 1:
            try:
                results[need[0]] = _normalize_intraday_frame(
                    raw.copy(), need[0], interval, period, tz
                )
            except Exception as e:
                errors.append(f"{need[0]}: {e}")
        else:
            errors.append("unexpected single-frame multi-symbol response")

    return results, errors


def latest_bars(df: pd.DataFrame, count: int = 5) -> pd.DataFrame:
    return df.tail(count).copy()
