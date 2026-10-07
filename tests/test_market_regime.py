from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from stockmarket.core import (
    AggregatedAction,
    MarketRegimeEvaluator,
    RegimeConfig,
    RegimeLabel,
    RegimeUnavailable,
    SignalAggregator,
    SignalInputs,
    regime_score,
)
from stockmarket.core.data import DataQualityError
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass


ZONE = ZoneInfo("America/New_York")
INDEX = pd.date_range("2026-10-05 09:30", periods=21, freq="5min", tz=ZONE)
AS_OF = INDEX[-1].to_pydatetime() + timedelta(minutes=5)


def instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def bars(closes):
    return pd.DataFrame(
        {
            "open": closes,
            "high": [p * 1.001 for p in closes],
            "low": [p * 0.999 for p in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=INDEX[:len(closes)],
    )


class MarketRegimeTests(unittest.TestCase):
    def setUp(self):
        self.instrument = instrument()
        self.evaluator = MarketRegimeEvaluator(
            RegimeConfig(lookback_bars=10, max_bar_age=timedelta(minutes=10)))

    def test_sustained_rise_is_uptrend_and_score_is_positive(self):
        assessment = self.evaluator.evaluate(
            self.instrument, bars([100 + i for i in range(21)]), as_of=AS_OF)
        self.assertIs(assessment.label, RegimeLabel.TRENDING_UP)
        self.assertGreater(assessment.directional_score, 0)
        self.assertGreaterEqual(assessment.directional_score, 0.35)
        self.assertGreaterEqual(assessment.volatility, 0)

    def test_sustained_fall_is_downtrend_and_score_is_negative(self):
        assessment = self.evaluator.evaluate(
            self.instrument, bars([120 - i for i in range(21)]), as_of=AS_OF)
        self.assertIs(assessment.label, RegimeLabel.TRENDING_DOWN)
        self.assertLess(assessment.directional_score, 0)

    def test_unchanged_prices_are_range_bound(self):
        assessment = self.evaluator.evaluate(
            self.instrument, bars([100.0] * 21), as_of=AS_OF)
        self.assertIs(assessment.label, RegimeLabel.RANGE_BOUND)
        self.assertEqual(assessment.directional_score, 0)
        self.assertEqual(assessment.volatility, 0)

    def test_high_volatility_is_separately_classified_without_losing_direction(self):
        closes = [100.0]
        for i in range(20):
            closes.append(closes[-1] * (1.08 if i % 2 == 0 else 0.92))
        evaluator = MarketRegimeEvaluator(
            RegimeConfig(
                lookback_bars=10,
                high_volatility_threshold=0.05,
                max_bar_age=timedelta(minutes=10),
            ))
        assessment = evaluator.evaluate(self.instrument, bars(closes), as_of=AS_OF)
        self.assertIs(assessment.label, RegimeLabel.HIGH_VOLATILITY)
        self.assertNotEqual(assessment.directional_score, 0)

    def test_insufficient_bars_are_explicitly_unavailable(self):
        with self.assertRaises(RegimeUnavailable):
            self.evaluator.evaluate(
                self.instrument, bars([100.0] * 8),
                as_of=INDEX[7].to_pydatetime() + timedelta(minutes=5))

    def test_stale_and_unsorted_data_are_rejected(self):
        with self.assertRaises(DataQualityError) as stale:
            self.evaluator.evaluate(
                self.instrument, bars([100 + i for i in range(21)]),
                as_of=AS_OF + timedelta(minutes=30))
        self.assertIn("STALE_BARS", stale.exception.issues)

        with self.assertRaises(DataQualityError) as unsorted:
            self.evaluator.evaluate(
                self.instrument, bars([100 + i for i in range(21)]).iloc[::-1],
                as_of=AS_OF)
        self.assertIn("UNSORTED_TIMESTAMPS", unsorted.exception.issues)

    def test_aware_evaluation_time_is_required(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.evaluator.evaluate(
                self.instrument, bars([100 + i for i in range(21)]),
                as_of=datetime(2026, 10, 5, 15, 0))

    def test_regime_score_checks_instrument_before_aggregation(self):
        assessment = self.evaluator.evaluate(
            self.instrument, bars([100 + i for i in range(21)]), as_of=AS_OF)
        self.assertEqual(
            regime_score(assessment, self.instrument.instrument_id),
            assessment.directional_score,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            regime_score(assessment, "XNAS:MSFT")

    def test_regime_score_can_feed_aggregation_as_supporting_evidence(self):
        assessment = self.evaluator.evaluate(
            self.instrument, bars([100 + i for i in range(21)]), as_of=AS_OF)
        inputs = SignalInputs(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol,
            timestamp=assessment.end_at,
            strategy="orb_vwap",
            technical=0.8,
            volume=0.5,
            momentum=0.6,
            regime=regime_score(assessment, self.instrument.instrument_id),
        )
        decision = SignalAggregator().aggregate(inputs, as_of=AS_OF)
        self.assertIs(decision.action, AggregatedAction.BUY)
        self.assertEqual(
            decision.explanation["inputs"]["regime"],
            assessment.directional_score,
        )

    def test_invalid_regime_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "lookback_bars"):
            RegimeConfig(lookback_bars=1)
        with self.assertRaisesRegex(ValueError, "high_volatility_threshold"):
            RegimeConfig(high_volatility_threshold=0)


if __name__ == "__main__":
    unittest.main()
