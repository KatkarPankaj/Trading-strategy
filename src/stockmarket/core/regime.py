"""Deterministic, provider-independent market-regime assessment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import isfinite, sqrt, tanh

import numpy as np
import pandas as pd

from .data import DataQualityError, validate_bars
from .data.provider import interval_delta
from .models import Instrument


class RegimeLabel(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


class RegimeUnavailable(ValueError):
    """The available, quality-checked bar window is insufficient for assessment."""


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    interval: str = "5m"
    lookback_bars: int = 20
    trend_threshold: float = 0.35
    high_volatility_threshold: float | None = None
    max_bar_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        interval_delta(self.interval)
        if isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int) \
                or self.lookback_bars < 2:
            raise ValueError("lookback_bars must be an integer of at least 2")
        if isinstance(self.trend_threshold, bool) or not isinstance(self.trend_threshold, (int, float)) \
                or not isfinite(self.trend_threshold) or not 0 < self.trend_threshold <= 1:
            raise ValueError("trend_threshold must be finite and in (0, 1]")
        if self.high_volatility_threshold is not None and (
                isinstance(self.high_volatility_threshold, bool)
                or not isinstance(self.high_volatility_threshold, (int, float))
                or not isfinite(self.high_volatility_threshold)
                or self.high_volatility_threshold <= 0):
            raise ValueError("high_volatility_threshold must be finite and positive")
        if not isinstance(self.max_bar_age, timedelta) or self.max_bar_age <= timedelta(0):
            raise ValueError("max_bar_age must be positive")


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    instrument_id: str
    label: RegimeLabel
    directional_score: float
    volatility: float
    interval: str
    lookback_bars: int
    start_at: datetime
    end_at: datetime
    assessed_at: datetime


class MarketRegimeEvaluator:
    """Assess recent price direction and per-bar volatility from validated OHLCV bars."""

    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def evaluate(
        self,
        instrument: Instrument,
        bars: pd.DataFrame,
        *,
        as_of: datetime,
    ) -> RegimeAssessment:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        report = validate_bars(
            bars,
            self.config.interval,
            timezone=instrument.timezone,
            now=as_of,
            max_age=self.config.max_bar_age,
        )
        if not report.ok:
            raise DataQualityError(report.issues)

        required_prices = self.config.lookback_bars + 1
        if len(bars) < required_prices:
            raise RegimeUnavailable(
                f"need {required_prices} bars, received {len(bars)}")
        closes = bars["close"].tail(required_prices).to_numpy(dtype=float)
        returns = np.diff(np.log(closes))
        if len(returns) < 2 or not np.isfinite(returns).all():
            raise RegimeUnavailable("insufficient finite returns for regime assessment")
        mean_return = float(returns.mean())
        volatility = float(returns.std(ddof=0))
        if volatility == 0:
            strength = 0.0 if mean_return == 0 else 1.0
        else:
            strength = tanh(mean_return * sqrt(len(returns)) / volatility)
        directional_score = max(-1.0, min(1.0, strength))

        if (self.config.high_volatility_threshold is not None
                and volatility > self.config.high_volatility_threshold):
            label = RegimeLabel.HIGH_VOLATILITY
        elif directional_score >= self.config.trend_threshold:
            label = RegimeLabel.TRENDING_UP
        elif directional_score <= -self.config.trend_threshold:
            label = RegimeLabel.TRENDING_DOWN
        else:
            label = RegimeLabel.RANGE_BOUND

        selected = bars.tail(required_prices)
        return RegimeAssessment(
            instrument_id=instrument.instrument_id,
            label=label,
            directional_score=directional_score,
            volatility=volatility,
            interval=self.config.interval,
            lookback_bars=self.config.lookback_bars,
            start_at=selected.index[0].to_pydatetime(),
            end_at=selected.index[-1].to_pydatetime(),
            assessed_at=as_of,
        )


def regime_score(assessment: RegimeAssessment, instrument_id: str) -> float:
    """Return a regime score for aggregation, rejecting cross-instrument reuse."""
    if not isinstance(assessment, RegimeAssessment):
        raise TypeError("assessment must be a RegimeAssessment")
    if assessment.instrument_id != instrument_id:
        raise ValueError("regime assessment instrument does not match aggregation inputs")
    return assessment.directional_score
