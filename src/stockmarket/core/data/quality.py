"""Data quality checks. Anything that fails here must not be traded on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from numbers import Real
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .provider import OHLCV, Quote, interval_delta

FROZEN_BARS = 5


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_quote(quote: object, now: datetime, max_age: timedelta) -> DataQualityReport:
    issues: list[str] = []
    if not isinstance(quote, Quote):
        return DataQualityReport(("INVALID_QUOTE",))
    if not _valid_number(quote.price) or quote.price <= 0:
        issues.append("INVALID_PRICE")
    if not isinstance(quote.instrument_id, str) or not quote.instrument_id.strip():
        issues.append("INVALID_INSTRUMENT_ID")
    if not isinstance(quote.provider, str) or not quote.provider.strip():
        issues.append("INVALID_PROVIDER")
    if not isinstance(quote.timestamp, datetime):
        issues.append("INVALID_TIMESTAMP")
    elif quote.timestamp.tzinfo is None or quote.timestamp.utcoffset() is None:
        issues.append("NAIVE_TIMESTAMP")
    else:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            issues.append("INVALID_VALIDATION_TIME")
        elif quote.timestamp > now:
            issues.append("TIMESTAMP_IN_FUTURE")
        elif isinstance(max_age, timedelta) and max_age > timedelta(0) \
                and now - quote.timestamp > max_age:
            issues.append("STALE_QUOTE")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        if "INVALID_VALIDATION_TIME" not in issues:
            issues.append("INVALID_VALIDATION_TIME")
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        issues.append("INVALID_MAX_AGE")
    for name, value in (("BID", quote.bid), ("ASK", quote.ask)):
        if value is not None and (not _valid_number(value) or value <= 0):
            issues.append(f"INVALID_{name}")
    if (quote.bid is None) != (quote.ask is None):
        issues.append("INCOMPLETE_SPREAD")
    elif _valid_number(quote.bid) and _valid_number(quote.ask) and quote.bid > quote.ask:
        issues.append("CROSSED_OR_INVALID_SPREAD")
    if quote.volume is not None and (not _valid_number(quote.volume) or quote.volume < 0):
        issues.append("INVALID_VOLUME")
    return DataQualityReport(tuple(issues))


def _valid_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)


def validate_bars(
    df: pd.DataFrame,
    interval: str,
    *,
    timezone: str,
    now: datetime | None = None,
    max_age: timedelta | None = None,
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> DataQualityReport:
    """Structural checks always; staleness only when `now` and `max_age` are given (live use)."""
    step = interval_delta(interval)
    issues: list[str] = []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return DataQualityReport(("NO_DATA",))
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        return DataQualityReport(("NAIVE_OR_INVALID_INDEX",))
    missing = set(OHLCV) - set(df.columns)
    if missing:
        return DataQualityReport((f"MISSING_COLUMNS:{','.join(sorted(missing))}",))

    if df.index.hasnans:
        issues.append("INVALID_TIMESTAMPS")
    if df.index.has_duplicates:
        issues.append("DUPLICATE_TIMESTAMPS")
    if not df.index.is_monotonic_increasing:
        issues.append("UNSORTED_TIMESTAMPS")
    try:
        values = df[list(OHLCV)].to_numpy(dtype=float, na_value=np.nan)
    except (TypeError, ValueError, OverflowError):
        values = np.array([], dtype=float)
        issues.append("INVALID_NUMERIC_VALUES")
    numeric = pd.DataFrame(values, columns=OHLCV, index=df.index) if values.size else None
    if values.size and not np.isfinite(values).all():
        issues.append("NON_FINITE_VALUES")
    elif values.size:
        assert numeric is not None
        if (numeric[["open", "high", "low", "close"]] <= 0).any().any() or (numeric["volume"] < 0).any():
            issues.append("NON_POSITIVE_VALUES")
        if (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1)).any() \
                or (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1)).any():
            issues.append("INCONSISTENT_OHLC")
        tail = numeric.tail(FROZEN_BARS)
        if len(tail) == FROZEN_BARS and tail["close"].nunique() == 1 and (tail["volume"] == 0).all():
            issues.append("FROZEN_PRICES")

    if interval != "1d" and "DUPLICATE_TIMESTAMPS" not in issues and "UNSORTED_TIMESTAMPS" not in issues:
        local = df.index.tz_convert(ZoneInfo(timezone))
        same_day = pd.Series(local.date, index=df.index)
        gaps = df.index.to_series().diff()
        within = same_day == same_day.shift()
        if bool(((gaps > step * 1.5) & within).any()):
            issues.append("MISSING_BARS")

    if (requested_start is None) != (requested_end is None):
        issues.append("INVALID_REQUEST_RANGE")
    elif requested_start is not None and requested_end is not None:
        if not _is_aware(requested_start) or not _is_aware(requested_end) \
                or requested_start >= requested_end:
            issues.append("INVALID_REQUEST_RANGE")
        elif not df.index.hasnans and (
                df.index.min() < requested_start or df.index.max() > requested_end):
            issues.append("OUT_OF_REQUEST_RANGE")

    if (now is None) != (max_age is None):
        issues.append("INVALID_FRESHNESS_POLICY")
    elif now is not None and max_age is not None:
        if not _is_aware(now):
            issues.append("INVALID_VALIDATION_TIME")
        elif max_age <= timedelta(0):
            issues.append("INVALID_MAX_AGE")
        else:
            last = df.index.max()
            if last > now:
                issues.append("TIMESTAMP_IN_FUTURE")
            elif now - (last + step) > max_age:
                issues.append("STALE_BARS")
    return DataQualityReport(tuple(issues))


def _is_aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
