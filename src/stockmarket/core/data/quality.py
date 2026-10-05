"""Data quality checks. Anything that fails here must not be traded on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
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


def validate_quote(quote: Quote, now: datetime, max_age: timedelta) -> DataQualityReport:
    issues: list[str] = []
    if isinstance(quote.price, bool) or not isinstance(quote.price, (int, float)) or not isfinite(quote.price) \
            or quote.price <= 0:
        issues.append("INVALID_PRICE")
    if quote.timestamp.tzinfo is None or quote.timestamp.utcoffset() is None:
        issues.append("NAIVE_TIMESTAMP")
    else:
        if quote.timestamp > now:
            issues.append("TIMESTAMP_IN_FUTURE")
        elif now - quote.timestamp > max_age:
            issues.append("STALE_QUOTE")
    if quote.bid is not None and quote.ask is not None and not (0 < quote.bid <= quote.ask):
        issues.append("CROSSED_OR_INVALID_SPREAD")
    return DataQualityReport(tuple(issues))


def validate_bars(
    df: pd.DataFrame,
    interval: str,
    *,
    timezone: str,
    now: datetime | None = None,
    max_age: timedelta | None = None,
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

    if df.index.has_duplicates:
        issues.append("DUPLICATE_TIMESTAMPS")
    if not df.index.is_monotonic_increasing:
        issues.append("UNSORTED_TIMESTAMPS")
    values = df[list(OHLCV)].to_numpy(dtype=float, na_value=np.nan)
    if not np.isfinite(values).all():
        issues.append("NON_FINITE_VALUES")
    else:
        if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df["volume"] < 0).any():
            issues.append("NON_POSITIVE_VALUES")
        if (df["high"] < df[["open", "low", "close"]].max(axis=1)).any() \
                or (df["low"] > df[["open", "high", "close"]].min(axis=1)).any():
            issues.append("INCONSISTENT_OHLC")
        tail = df.tail(FROZEN_BARS)
        if len(tail) == FROZEN_BARS and tail["close"].nunique() == 1 and (tail["volume"] == 0).all():
            issues.append("FROZEN_PRICES")

    if interval != "1d" and "DUPLICATE_TIMESTAMPS" not in issues and "UNSORTED_TIMESTAMPS" not in issues:
        local = df.index.tz_convert(ZoneInfo(timezone))
        same_day = pd.Series(local.date, index=df.index)
        gaps = df.index.to_series().diff()
        within = same_day == same_day.shift()
        if bool(((gaps > step * 1.5) & within).any()):
            issues.append("MISSING_BARS")

    if now is not None and max_age is not None:
        last = df.index.max()
        if last > now:
            issues.append("TIMESTAMP_IN_FUTURE")
        elif now - (last + step) > max_age:
            issues.append("STALE_BARS")
    return DataQualityReport(tuple(issues))
