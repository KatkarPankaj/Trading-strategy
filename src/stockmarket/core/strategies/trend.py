"""Completed-bar moving-average crossover, independent of opening-range levels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from ..data import DataQualityError, validate_bars
from ..data.provider import interval_delta
from ..market_session import MarketSession
from ..models import AssetClass, Instrument, Signal, SignalSide
from .base import StrategyMetadata
from .orb_vwap import OrbVwapConfig, entry_signal


@dataclass(frozen=True, slots=True)
class TrendConfig:
    interval: str = "5m"
    fast_window: int = 5
    slow_window: int = 20
    volume_ma_window: int = 20
    volume_spike_threshold: float = 1.0
    allow_short: bool = False
    stop_loss_pct: float = .004
    take_profit_pct: float = .008
    max_bar_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.interval not in {"5m", "15m"}:
            raise ValueError("trend requires 5m or 15m completed OHLCV bars")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (
                self.fast_window, self.slow_window)) \
                or not 2 <= self.fast_window < self.slow_window <= 100:
            raise ValueError("trend requires 2 <= fast_window < slow_window <= 100")
        OrbVwapConfig(
            interval=self.interval, volume_ma_window=self.volume_ma_window,
            volume_spike_threshold=self.volume_spike_threshold, allow_short=self.allow_short,
            stop_loss_pct=self.stop_loss_pct, take_profit_pct=self.take_profit_pct,
            max_bar_age=self.max_bar_age)


class TrendStrategy:
    version = "1.0.0"
    metadata = StrategyMetadata(
        version="1.0.0", supported_markets=frozenset({"*"}),
        supported_asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}))

    def __init__(self, config: TrendConfig | None = None) -> None:
        self.config = config or TrendConfig()

    @property
    def name(self) -> str:
        return "sma_trend"

    def evaluate(
        self, instrument: Instrument, bars: pd.DataFrame,
        session: MarketSession, *, as_of: datetime,
    ) -> Signal:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if instrument.timezone != session.timezone:
            raise ValueError("session timezone must match instrument")
        report = validate_bars(bars, self.config.interval, timezone=instrument.timezone,
                               now=as_of, max_age=self.config.max_bar_age)
        if not report.ok:
            raise DataQualityError(report.issues)
        at = bars.index[-1].to_pydatetime()

        def hold(reason: str) -> Signal:
            return Signal(instrument_id=instrument.instrument_id, symbol=instrument.symbol,
                          timestamp=at, strategy=self.name, side=SignalSide.HOLD, reasons=(reason,))

        if at + interval_delta(self.config.interval) > as_of:
            return hold("INCOMPLETE_BAR")
        local = bars.index.tz_convert(session.zone)
        time = local[-1].time().replace(tzinfo=None)
        if not session.market_open <= time < session.market_close \
                or not session.is_before_entry_cutoff(at):
            return hold("OUTSIDE_ENTRY_WINDOW")
        day = bars.loc[(local.date == local[-1].date())
                       & (local.time >= session.market_open) & (local.time < session.market_close)]
        required = max(self.config.slow_window, self.config.volume_ma_window) + 1
        if len(day) < required:
            return hold("INSUFFICIENT_TREND_HISTORY")
        if not day.index.to_series().diff().dropna().eq(interval_delta(self.config.interval)).all():
            return hold("GAPPED_TREND_HISTORY")
        fast = day.close.rolling(self.config.fast_window).mean()
        slow = day.close.rolling(self.config.slow_window).mean()
        close = float(day.close.iloc[-1])
        if not instrument.is_valid_price(close):
            return hold("PRICE_OFF_TICK")
        prior_volume = float(day.volume.iloc[-self.config.volume_ma_window - 1:-1].mean())
        if prior_volume <= 0 or float(day.volume.iloc[-1]) < prior_volume * self.config.volume_spike_threshold:
            return hold("VOLUME_NOT_CONFIRMED")
        long = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        short = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
        if not long and not (short and self.config.allow_short and instrument.shortable is True):
            return hold("NO_TREND_CROSSOVER")
        # Share trusted tick-rounded stop/target construction, not ORB signal rules.
        return entry_signal(
            self.name, instrument, at, SignalSide.BUY if long else SignalSide.SELL, close,
            ("SMA_BULLISH_CROSSOVER" if long else "SMA_BEARISH_CROSSOVER", "PRIOR_VOLUME_CONFIRMED"),
            stop_loss_pct=self.config.stop_loss_pct, take_profit_pct=self.config.take_profit_pct,
            invalidation=("Trend crossover is not sustained; stop/target or session flatten exits",))
