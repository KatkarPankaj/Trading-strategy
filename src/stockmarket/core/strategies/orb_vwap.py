"""Opening-range breakout with VWAP and volume confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import ceil, isfinite

import pandas as pd

from ..data import DataQualityError, validate_bars
from ..data.provider import interval_delta
from ..market_session import MarketSession
from ..models import AssetClass, Instrument, Signal, SignalSide
from .base import StrategyMetadata


@dataclass(frozen=True, slots=True)
class OrbVwapConfig:
    interval: str = "5m"
    volume_ma_window: int = 20
    volume_spike_threshold: float = 1.2
    vwap_price_source: str = "typical"
    allow_short: bool = False
    stop_loss_pct: float = 0.004
    take_profit_pct: float = 0.008
    max_bar_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        interval_delta(self.interval)
        if self.interval == "1d":
            raise ValueError("ORB/VWAP requires an intraday interval")
        if isinstance(self.volume_ma_window, bool) or not isinstance(self.volume_ma_window, int) \
                or self.volume_ma_window < 1:
            raise ValueError("volume_ma_window must be a positive integer")
        if isinstance(self.volume_spike_threshold, bool) \
                or not isinstance(self.volume_spike_threshold, (int, float)) \
                or not isfinite(self.volume_spike_threshold) or self.volume_spike_threshold <= 0:
            raise ValueError("volume_spike_threshold must be finite and positive")
        if self.vwap_price_source not in ("typical", "close"):
            raise ValueError("vwap_price_source must be 'typical' or 'close'")
        if not isinstance(self.allow_short, bool):
            raise TypeError("allow_short must be a bool")
        for name, value in (("stop_loss_pct", self.stop_loss_pct),
                            ("take_profit_pct", self.take_profit_pct)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.stop_loss_pct >= 1:
            raise ValueError("stop_loss_pct must be less than 1")
        if not isinstance(self.max_bar_age, timedelta) or self.max_bar_age <= timedelta(0):
            raise ValueError("max_bar_age must be positive")


class OrbVwapStrategy:
    """Produce one deterministic Signal for the most recent eligible bar."""

    version = "1.0.0"
    metadata = StrategyMetadata(
        version="1.0.0",
        supported_markets=frozenset({"*"}),
        supported_asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
    )

    def __init__(self, config: OrbVwapConfig | None = None) -> None:
        self.config = config or OrbVwapConfig()

    @property
    def name(self) -> str:
        return "orb_vwap"

    def evaluate(
        self,
        instrument: Instrument,
        bars: pd.DataFrame,
        session: MarketSession,
        *,
        as_of: datetime,
    ) -> Signal:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if session.timezone != instrument.timezone:
            raise ValueError("session timezone must match instrument timezone")

        report = validate_bars(
            bars,
            self.config.interval,
            timezone=instrument.timezone,
            now=as_of,
            max_age=self.config.max_bar_age,
        )
        if not report.ok:
            raise DataQualityError(report.issues)

        local_index = bars.index.tz_convert(session.zone)
        latest = bars.iloc[-1]
        latest_at = bars.index[-1].to_pydatetime()
        latest_local = local_index[-1]
        latest_time = latest_local.time().replace(tzinfo=None)
        if not session.market_open <= latest_time < session.market_close:
            return self._hold(instrument, latest_at, "OUTSIDE_MARKET_SESSION")

        day_mask = local_index.date == latest_local.date()
        day_bars = bars.loc[day_mask]
        day_index = local_index[day_mask]
        range_mask = [
            session.market_open <= at.time().replace(tzinfo=None) < session.opening_range_end
            for at in day_index
        ]
        opening_range = day_bars.loc[range_mask]
        interval = interval_delta(self.config.interval)
        range_duration = datetime.combine(
            latest_local.date(), session.opening_range_end
        ) - datetime.combine(latest_local.date(), session.market_open)
        expected_range_bars = ceil(range_duration / interval)
        if expected_range_bars < 1 or len(opening_range) < expected_range_bars:
            return self._hold(instrument, latest_at, "OPENING_RANGE_INCOMPLETE")
        if latest_time < session.opening_range_end:
            return self._hold(instrument, latest_at, "OPENING_RANGE_ACTIVE")
        if not session.is_before_entry_cutoff(latest_at):
            return self._hold(instrument, latest_at, "OUTSIDE_ENTRY_WINDOW")

        close = float(latest["close"])
        if not instrument.is_valid_price(close):
            return self._hold(instrument, latest_at, "PRICE_OFF_TICK")
        if self.config.vwap_price_source == "typical":
            prices = (day_bars["high"] + day_bars["low"] + day_bars["close"]) / 3.0
        else:
            prices = day_bars["close"]
        cumulative_volume = day_bars["volume"].cumsum()
        total_volume = float(cumulative_volume.iloc[-1])
        if total_volume <= 0:
            return self._hold(instrument, latest_at, "ZERO_SESSION_VOLUME")
        vwap = float((prices * day_bars["volume"]).sum() / total_volume)
        volume_average = float(
            bars["volume"].rolling(self.config.volume_ma_window, min_periods=1).mean().iloc[-1])
        volume_spike = float(latest["volume"]) >= (
            volume_average * self.config.volume_spike_threshold)
        opening_high = float(opening_range["high"].max())
        opening_low = float(opening_range["low"].min())
        long_setup = close > opening_high and close > vwap and volume_spike
        short_setup = self.config.allow_short and close < opening_low and close < vwap and volume_spike

        if long_setup:
            return self._entry_signal(
                instrument, latest_at, SignalSide.BUY, close,
                ("ORB_BREAKOUT_ABOVE", "CLOSE_ABOVE_VWAP", "VOLUME_SPIKE"))
        if short_setup:
            return self._entry_signal(
                instrument, latest_at, SignalSide.SELL, close,
                ("ORB_BREAKOUT_BELOW", "CLOSE_BELOW_VWAP", "VOLUME_SPIKE"))
        return self._hold(instrument, latest_at, "NO_CONFIRMED_BREAKOUT")

    def _entry_signal(
        self,
        instrument: Instrument,
        timestamp: datetime,
        side: SignalSide,
        entry: float,
        reasons: tuple[str, ...],
    ) -> Signal:
        if side is SignalSide.BUY:
            stop = _tick_price(
                entry * (1 - self.config.stop_loss_pct), instrument.tick_size, ROUND_FLOOR)
            target = _tick_price(
                entry * (1 + self.config.take_profit_pct), instrument.tick_size, ROUND_CEILING)
        else:
            stop = _tick_price(
                entry * (1 + self.config.stop_loss_pct), instrument.tick_size, ROUND_CEILING)
            target = _tick_price(
                entry * (1 - self.config.take_profit_pct), instrument.tick_size, ROUND_FLOOR)
        if side is SignalSide.BUY and not 0 < stop < entry < target:
            return self._hold(instrument, timestamp, "INVALID_TICK_ADJUSTED_RISK_PRICES")
        if side is SignalSide.SELL and not 0 < target < entry < stop:
            return self._hold(instrument, timestamp, "INVALID_TICK_ADJUSTED_RISK_PRICES")
        risk = abs(entry - stop)
        reward = abs(target - entry)
        return Signal(
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            timestamp=timestamp,
            strategy=self.name,
            side=side,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            reward_risk=reward / risk,
            reasons=reasons,
            invalidation_conditions=("Closing price crosses VWAP against the signal",),
        )

    def _hold(self, instrument: Instrument, timestamp: datetime, reason: str) -> Signal:
        return Signal(
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            timestamp=timestamp,
            strategy=self.name,
            side=SignalSide.HOLD,
            reasons=(reason,),
        )


def _tick_price(price: float, tick_size: float, rounding: str) -> float:
    tick = Decimal(str(tick_size))
    units = (Decimal(str(price)) / tick).to_integral_value(rounding=rounding)
    return float(units * tick)
