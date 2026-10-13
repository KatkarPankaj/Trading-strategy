from dataclasses import replace
from datetime import time, timedelta
import unittest

import pandas as pd

from stockmarket.core.ai.schemas import StrategyProposalSchema
from stockmarket.core.backtesting.engine import BacktestConfig, BacktestEngine, BacktestSignal
from stockmarket.core.data import DataQualityError
from stockmarket.core.market_session import MarketSession
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, OrderSide, SignalSide, TradeIntent
from stockmarket.core.sizing import SizingLimits
from stockmarket.core.strategies.trend import TrendConfig, TrendStrategy

from test_strategy_lab import specification


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.market = default_markets().get("US")
        self.instrument = replace(self.market.instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=.01), shortable=True)
        self.session = MarketSession(
            timezone=self.market.timezone, market_open=time(9, 30),
            opening_range_end=time(9, 45), entry_cutoff=time(15),
            square_off=time(15, 55), market_close=time(16), late_entry_start=time(12))
        self.strategy = TrendStrategy(TrendConfig(fast_window=2, slow_window=5, volume_ma_window=5,
                                                 allow_short=True))

    def bars(self, values):
        index = pd.date_range("2026-10-08 09:30", periods=len(values),
                              freq="5min", tz="America/New_York")
        return pd.DataFrame({"open": values, "close": values,
                             "high": [v + .1 for v in values], "low": [v - .1 for v in values],
                             "volume": [1000.] * len(values)}, index=index)

    def test_causal_long_and_short_tick_rounded_stop_target(self):
        for values, intent in (([100.] * 5 + [101.], TradeIntent.BUY),
                               ([100.] * 5 + [99.], TradeIntent.SHORT_SELL)):
            with self.subTest(intent=intent):
                bars = self.bars(values)
                signal = self.strategy.evaluate(self.instrument, bars, self.session,
                                                as_of=bars.index[-1].to_pydatetime() + timedelta(minutes=5))
                self.assertEqual(signal.intent, intent)
                self.assertTrue(self.instrument.is_valid_price(signal.stop_loss))
                self.assertTrue(self.instrument.is_valid_price(signal.take_profit))

    def test_no_crossover_incomplete_gap_and_short_eligibility_do_not_enter(self):
        for values, instrument, expected in (
            ([100.] * 6, self.instrument, "NO_TREND_CROSSOVER"),
            ([100.] * 5 + [99.], replace(self.instrument, shortable=False), "NO_TREND_CROSSOVER")):
            bars = self.bars(values)
            signal = self.strategy.evaluate(instrument, bars, self.session,
                                            as_of=bars.index[-1].to_pydatetime() + timedelta(minutes=5))
            self.assertEqual(signal.side, SignalSide.HOLD)
            self.assertEqual(signal.reasons, (expected,))
        bars = self.bars([100.] * 5 + [101.])
        self.assertEqual(self.strategy.evaluate(
            self.instrument, bars, self.session, as_of=bars.index[-1].to_pydatetime()).reasons,
            ("INCOMPLETE_BAR",))
        self.assertEqual(self.strategy.evaluate(
            self.instrument, bars.iloc[:3], self.session,
            as_of=bars.index[2].to_pydatetime() + timedelta(minutes=5)).reasons,
            ("INSUFFICIENT_TREND_HISTORY",))
        bars = self.bars([100.] * 6 + [101.]).drop(self.bars([100.] * 7).index[3])
        with self.assertRaisesRegex(DataQualityError, "MISSING_BARS"):
            self.strategy.evaluate(self.instrument, bars, self.session,
                                   as_of=bars.index[-1].to_pydatetime() + timedelta(minutes=5))

    def test_invalid_stale_and_volume_rejected(self):
        bars = self.bars([100.] * 5 + [101.])
        with self.assertRaises(DataQualityError):
            self.strategy.evaluate(self.instrument, bars, self.session,
                                   as_of=bars.index[-1].to_pydatetime() + timedelta(hours=1))
        bars.iloc[-1, bars.columns.get_loc("volume")] = 0
        self.assertEqual(self.strategy.evaluate(
            self.instrument, bars, self.session,
            as_of=bars.index[-1].to_pydatetime() + timedelta(minutes=5)).reasons,
            ("VOLUME_NOT_CONFIRMED",))

    def test_bounded_proposal_and_template_rule_matching(self):
        spec = specification()
        spec.update(template="sma_trend", entry_rule="SMA_CROSSOVER_WITH_PRIOR_VOLUME")
        parsed = StrategyProposalSchema.model_validate(spec)
        self.assertEqual(parsed.parameters.fast_window, 5)
        from pydantic import ValidationError
        spec["parameters"].update(fast_window=20, slow_window=5)
        with self.assertRaises(ValidationError):
            StrategyProposalSchema.model_validate(spec)
        with self.assertRaises(ValueError):
            TrendConfig(fast_window=True)
