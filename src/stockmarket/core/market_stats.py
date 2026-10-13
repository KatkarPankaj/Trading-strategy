"""Deterministic liquidity, slippage and correlation inputs for portfolio risk checks.

Every figure is derived from validated daily bars. When data is missing or insufficient the
corresponding key is omitted so that the RiskEngine fails closed with an ``*_UNKNOWN`` code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite, sqrt
from threading import RLock
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from .models import Instrument

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketStatsSettings:
    lookback_days: int = 20
    calendar_padding_days: int = 25
    correlation_threshold: float = 0.7
    half_spread_bps: float = 2.0
    impact_coefficient: float = 1.0
    reference_order_notional: float = 25_000.0

    def __post_init__(self) -> None:
        if not 5 <= self.lookback_days <= 250:
            raise ValueError("lookback_days must be between 5 and 250")
        if self.calendar_padding_days < 0:
            raise ValueError("calendar_padding_days must be non-negative")
        if not 0.0 < self.correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be in (0, 1]")
        for name in ("half_spread_bps", "impact_coefficient", "reference_order_notional"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


class MarketStatsProvider:
    """Callable ``market_stats(instrument_id) -> dict`` for TradeProposalService/TradingService."""

    def __init__(
        self,
        *,
        market_data: Any,
        instruments: Mapping[str, Instrument],
        portfolio: Any,
        settings: MarketStatsSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_data = market_data
        self.instruments = instruments
        self.portfolio = portfolio
        self.settings = settings or MarketStatsSettings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, date], pd.DataFrame | None] = {}
        self._lock = RLock()

    def __call__(self, instrument_id: str) -> dict[str, float]:
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            return {}
        bars = self._daily_bars(instrument)
        if bars is None:
            return {}
        stats: dict[str, float] = {}
        traded_value = (bars["close"] * bars["volume"]).astype(float)
        rate = self._rate(instrument.currency)
        adv = float(traded_value.mean()) * rate if rate is not None else None
        if adv is not None and isfinite(adv) and adv > 0:
            stats["average_daily_traded_value"] = adv
            returns = bars["close"].astype(float).pct_change().dropna()
            sigma = float(returns.std(ddof=1)) if len(returns) >= 2 else float("nan")
            if isfinite(sigma) and sigma >= 0:
                participation = self.settings.reference_order_notional / adv
                impact = self.settings.impact_coefficient * sigma * sqrt(participation) * 10_000
                stats["expected_slippage_bps"] = round(
                    self.settings.half_spread_bps + impact, 4)
        correlated = self._correlated_exposure(instrument, bars)
        if correlated is not None:
            stats["correlated_exposure"] = correlated
        return stats

    def _correlated_exposure(self, instrument: Instrument, bars: pd.DataFrame) -> float | None:
        positions = self.portfolio.positions()
        if not positions:
            return 0.0
        candidate = _daily_returns(bars)
        total = 0.0
        for held_id, position in positions.items():
            rate = self._rate(position.currency)
            if rate is None:
                return None
            notional = abs(position.quantity) * position.last_price * rate
            if held_id == instrument.instrument_id:
                total += notional
                continue
            held_instrument = self.instruments.get(held_id)
            held_bars = self._daily_bars(held_instrument) if held_instrument else None
            if held_bars is None:
                return None
            aligned = pd.concat(
                [candidate, _daily_returns(held_bars)], axis=1, join="inner").dropna()
            if len(aligned) < self.settings.lookback_days // 2:
                return None
            corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
            if not isfinite(corr):
                return None
            if corr >= self.settings.correlation_threshold:
                total += notional
        return total

    def _daily_bars(self, instrument: Instrument) -> pd.DataFrame | None:
        now = self.clock().astimezone(timezone.utc)
        key = (instrument.instrument_id, now.date())
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        start = now - timedelta(
            days=self.settings.lookback_days + self.settings.calendar_padding_days)
        try:
            df = self.market_data.get_ohlcv(instrument, "1d", start, now)
        except Exception as exc:  # noqa: BLE001 - any data failure must fail closed
            log.warning("daily bars unavailable for market stats: %s (%s)",
                        instrument.instrument_id, type(exc).__name__)
            return None  # transient failures are not cached
        bars = self._complete_sessions(instrument, df, now)
        with self._lock:
            self._cache[key] = bars
        return bars

    def _complete_sessions(
        self, instrument: Instrument, df: Any, now: datetime,
    ) -> pd.DataFrame | None:
        if not isinstance(df, pd.DataFrame) or df.empty \
                or not {"close", "volume"}.issubset(df.columns):
            return None
        zone = ZoneInfo(instrument.timezone)
        local_today = now.astimezone(zone).date()
        index = pd.DatetimeIndex(df.index)
        local_dates = index.tz_convert(zone).date if index.tz is not None else index.date
        complete = df[[d < local_today for d in local_dates]]
        complete = complete[["close", "volume"]].dropna()
        complete = complete[(complete["close"] > 0) & (complete["volume"] >= 0)]
        if len(complete) < self.settings.lookback_days:
            return None
        return complete.tail(self.settings.lookback_days)

    def _rate(self, currency: str) -> float | None:
        try:
            rate = float(self.portfolio.rate_to_base(currency))
        except Exception:  # noqa: BLE001 - unknown FX must fail closed
            return None
        return rate if isfinite(rate) and rate > 0 else None


def _daily_returns(bars: pd.DataFrame) -> pd.Series:
    index = pd.DatetimeIndex(bars.index)
    series = pd.Series(bars["close"].astype(float).to_numpy(),
                       index=[ts.date() for ts in index])
    return series.pct_change()
