from datetime import time, timedelta
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from stockmarket.core import (
    AggregatedAction,
    MarketSession,
    OrbVwapConfig,
    OrbVwapStrategy,
    SignalAggregator,
    SignalInputs,
    SignalSide,
    Strategy,
)
from stockmarket.core.data import DataQualityError
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass


ZONE = ZoneInfo("America/New_York")
BAR_TIMES = pd.date_range(
    "2026-10-05 09:30", periods=5, freq="5min", tz=ZONE)


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def make_session():
    return MarketSession(
        timezone="America/New_York",
        market_open=time(9, 30),
        opening_range_end=time(9, 45),
        entry_cutoff=time(15, 0),
        square_off=time(15, 55),
        market_close=time(16, 0),
        late_entry_start=time(12, 0),
    )


def make_bars(*, direction="up"):
    if direction == "up":
        highs = [100.0, 101.0, 102.0, 103.0, 104.0]
        lows = [98.0, 99.0, 100.0, 101.0, 102.0]
        closes = [99.0, 100.0, 101.0, 102.0, 103.0]
    else:
        highs = [102.0, 101.0, 100.0, 99.0, 98.0]
        lows = [100.0, 99.0, 98.0, 97.0, 96.0]
        closes = [101.0, 100.0, 99.0, 98.0, 97.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0, 100.0, 100.0, 100.0, 500.0],
        },
        index=BAR_TIMES,
    )


AS_OF = BAR_TIMES[-1].to_pydatetime() + timedelta(minutes=5)


class OrbVwapStrategyTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()
        self.session = make_session()
        self.strategy = OrbVwapStrategy()

    def test_implements_shared_strategy_contract(self):
        self.assertIsInstance(self.strategy, Strategy)
        self.assertEqual(self.strategy.name, "orb_vwap")

    def test_confirmed_long_breakout_returns_priced_signal(self):
        signal = self.strategy.evaluate(
            self.instrument, make_bars(), self.session, as_of=AS_OF)
        self.assertIs(signal.side, SignalSide.BUY)
        self.assertEqual(signal.strategy, "orb_vwap")
        self.assertEqual(signal.entry_price, 103.0)
        self.assertTrue(self.instrument.is_valid_price(signal.stop_loss))
        self.assertTrue(self.instrument.is_valid_price(signal.take_profit))
        self.assertLess(signal.stop_loss, signal.entry_price)
        self.assertGreater(signal.take_profit, signal.entry_price)
        self.assertIn("VOLUME_SPIKE", signal.reasons)

    def test_strategy_signal_connects_to_research_evidence_aggregation(self):
        signal = self.strategy.evaluate(
            self.instrument, make_bars(), self.session, as_of=AS_OF)
        evidence = SignalInputs(
            instrument_id=signal.instrument_id,
            symbol=signal.symbol,
            timestamp=signal.timestamp,
            strategy=signal.strategy,
            technical=None,
            volume=0.8,
            momentum=0.7,
            regime=0.5,
            sector=None,
            news=0.4,
            fundamental=None,
            history=None,
        )
        decision = SignalAggregator().aggregate(
            evidence, as_of=AS_OF, strategy_signal=signal)
        self.assertIs(decision.action, AggregatedAction.BUY)
        self.assertEqual(decision.explanation["inputs"]["technical"], 1.0)
        self.assertEqual(
            decision.explanation["inputs"]["strategy_signal"]["signal_id"],
            str(signal.signal_id),
        )

    def test_short_is_disabled_by_default_and_requires_explicit_opt_in(self):
        bars = make_bars(direction="down")
        default = self.strategy.evaluate(
            self.instrument, bars, self.session, as_of=AS_OF)
        enabled = OrbVwapStrategy(OrbVwapConfig(allow_short=True)).evaluate(
            self.instrument, bars, self.session, as_of=AS_OF)
        self.assertIs(default.side, SignalSide.HOLD)
        self.assertIs(enabled.side, SignalSide.SELL)
        self.assertGreater(enabled.stop_loss, enabled.entry_price)
        self.assertLess(enabled.take_profit, enabled.entry_price)

    def test_no_breakout_returns_unpriced_hold(self):
        bars = make_bars()
        bars.loc[BAR_TIMES[-1], ["high", "low", "close", "open"]] = [102.0, 100.0, 101.0, 101.0]
        signal = self.strategy.evaluate(
            self.instrument, bars, self.session, as_of=AS_OF)
        self.assertIs(signal.side, SignalSide.HOLD)
        self.assertIsNone(signal.entry_price)
        self.assertEqual(signal.reasons, ("NO_CONFIRMED_BREAKOUT",))

    def test_incomplete_opening_range_returns_hold(self):
        bars = make_bars().iloc[:2]
        as_of = BAR_TIMES[1].to_pydatetime() + timedelta(minutes=5)
        signal = self.strategy.evaluate(
            self.instrument, bars, self.session, as_of=as_of)
        self.assertIs(signal.side, SignalSide.HOLD)
        self.assertEqual(signal.reasons, ("OPENING_RANGE_INCOMPLETE",))

    def test_stale_or_malformed_bars_raise_quality_error(self):
        stale_as_of = AS_OF + timedelta(minutes=30)
        with self.assertRaises(DataQualityError) as stale:
            self.strategy.evaluate(
                self.instrument, make_bars(), self.session, as_of=stale_as_of)
        self.assertIn("STALE_BARS", stale.exception.issues)

        unsorted = make_bars().iloc[::-1]
        with self.assertRaises(DataQualityError) as invalid:
            self.strategy.evaluate(
                self.instrument, unsorted, self.session, as_of=AS_OF)
        self.assertIn("UNSORTED_TIMESTAMPS", invalid.exception.issues)

    def test_session_timezone_must_match_instrument(self):
        other_session = MarketSession(
            timezone="UTC",
            market_open=time(9, 30),
            opening_range_end=time(9, 45),
            entry_cutoff=time(15, 0),
            square_off=time(15, 55),
            market_close=time(16, 0),
            late_entry_start=time(12, 0),
        )
        with self.assertRaisesRegex(ValueError, "timezone must match"):
            self.strategy.evaluate(
                self.instrument, make_bars(), other_session, as_of=AS_OF)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "intraday interval"):
            OrbVwapConfig(interval="1d")
        with self.assertRaisesRegex(ValueError, "stop_loss_pct"):
            OrbVwapConfig(stop_loss_pct=1.0)
        with self.assertRaisesRegex(ValueError, "volume_ma_window"):
            OrbVwapConfig(volume_ma_window=0)


if __name__ == "__main__":
    unittest.main()
