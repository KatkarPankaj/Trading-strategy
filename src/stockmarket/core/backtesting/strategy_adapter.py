"""Use the same trusted deterministic template in laboratory and comparison runs."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from ..data.provider import interval_delta
from ..market_session import MarketSession
from ..models import Instrument, OrderSide, TradeIntent
from ..strategies.base import Strategy
from .engine import BacktestSignal, SignalFn


def strategy_signal_fn(strategy: Strategy, instrument: Instrument, session: MarketSession) -> SignalFn:
    config = getattr(strategy, "config")
    step = interval_delta(config.interval)

    def signal_fn(iid: str, history: pd.DataFrame) -> BacktestSignal | None:
        if iid != instrument.instrument_id:
            raise ValueError("historical signal instrument mismatch")
        at = history.index[-1].to_pydatetime() + step
        local_day = history.index[-1].tz_convert(instrument.timezone).date()
        first = history.index.searchsorted(datetime.combine(
            local_day, session.market_open, ZoneInfo(instrument.timezone)))
        history = history.iloc[max(0, first - config.volume_ma_window):]
        signal = strategy.evaluate(instrument, history, session, as_of=at)
        if signal.intent not in {TradeIntent.BUY, TradeIntent.SHORT_SELL}:
            return None
        if signal.stop_loss is None:
            raise ValueError("deterministic entry lacks a stop")
        return BacktestSignal(
            OrderSide.BUY if signal.intent is TradeIntent.BUY else OrderSide.SELL,
            signal.stop_loss, signal.take_profit, "; ".join(signal.reasons))

    return signal_fn
