from datetime import datetime, timedelta, timezone
import json
import unittest

from stockmarket.core import (
    AggregatedAction,
    AggregationConfig,
    SignalAggregator,
    SignalInputs,
    SignalSide,
)

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)


def inputs(**overrides):
    values = dict(
        instrument_id="XNSE:RELIANCE",
        symbol="RELIANCE",
        timestamp=NOW - timedelta(seconds=30),
        strategy="orb",
        technical=0.8,
        volume=0.6,
        momentum=0.7,
        regime=0.5,
        sector=0.4,
        news=0.2,
        fundamental=0.3,
        history=0.5,
        history_trades=50,
        volatility=0.02,
        liquidity=1e9,
    )
    values.update(overrides)
    return SignalInputs(**values)


class SignalAggregationTests(unittest.TestCase):
    def setUp(self):
        self.aggregator = SignalAggregator()

    def test_bullish_inputs_buy(self):
        d = self.aggregator.aggregate(inputs(), as_of=NOW)
        self.assertIs(d.action, AggregatedAction.BUY)
        self.assertGreaterEqual(d.confidence, 40)
        self.assertLessEqual(d.confidence, 100)

    def test_bearish_inputs_sell(self):
        bearish = inputs(technical=-0.8, volume=-0.6, momentum=-0.7, regime=-0.5,
                         sector=-0.4, news=-0.2, fundamental=-0.3, history=-0.5)
        d = self.aggregator.aggregate(bearish, as_of=NOW)
        self.assertIs(d.action, AggregatedAction.SELL)

    def test_neutral_inputs_hold(self):
        flat = inputs(technical=0.0, volume=0.0, momentum=0.0, regime=0.0,
                      sector=0.0, news=0.0, fundamental=0.0, history=0.0)
        d = self.aggregator.aggregate(flat, as_of=NOW)
        self.assertIs(d.action, AggregatedAction.HOLD)

    def test_decision_is_reproducible(self):
        a = self.aggregator.aggregate(inputs(), as_of=NOW)
        b = SignalAggregator().aggregate(inputs(), as_of=NOW)
        self.assertEqual(a.input_hash, b.input_hash)
        self.assertEqual(a.decision_id, b.decision_id)
        self.assertEqual(a.explanation_json(), b.explanation_json())
        json.loads(a.explanation_json())

    def test_changed_input_changes_hash(self):
        a = self.aggregator.aggregate(inputs(), as_of=NOW)
        b = self.aggregator.aggregate(inputs(news=0.3), as_of=NOW)
        self.assertNotEqual(a.input_hash, b.input_hash)

    def test_stale_inputs_skip(self):
        d = self.aggregator.aggregate(
            inputs(timestamp=NOW - timedelta(hours=1)), as_of=NOW)
        self.assertIs(d.action, AggregatedAction.SKIP)
        self.assertIn("STALE_INPUTS", d.reason_codes)
        self.assertEqual(d.confidence, 0)

    def test_future_inputs_skip(self):
        d = self.aggregator.aggregate(
            inputs(timestamp=NOW + timedelta(seconds=5)), as_of=NOW)
        self.assertIn("INPUT_FROM_FUTURE", d.reason_codes)

    def test_missing_required_component_skips(self):
        d = self.aggregator.aggregate(inputs(technical=None), as_of=NOW)
        self.assertIs(d.action, AggregatedAction.SKIP)
        self.assertIn("MISSING_REQUIRED_TECHNICAL", d.reason_codes)

    def test_too_few_components_skip(self):
        sparse = inputs(volume=None, momentum=None, regime=None, sector=None,
                        news=None, fundamental=None, history=None)
        d = self.aggregator.aggregate(sparse, as_of=NOW)
        self.assertIn("INSUFFICIENT_COMPONENTS", d.reason_codes)

    def test_low_liquidity_skips(self):
        agg = SignalAggregator(AggregationConfig(min_liquidity=1e6))
        self.assertIs(agg.aggregate(inputs(liquidity=10.0), as_of=NOW).action,
                      AggregatedAction.SKIP)
        self.assertIs(agg.aggregate(inputs(liquidity=None), as_of=NOW).action,
                      AggregatedAction.SKIP)

    def test_excessive_volatility_skips(self):
        d = self.aggregator.aggregate(inputs(volatility=0.5), as_of=NOW)
        self.assertIn("EXCESSIVE_VOLATILITY", d.reason_codes)

    def test_thin_history_is_excluded_not_trusted(self):
        d = self.aggregator.aggregate(inputs(history_trades=3), as_of=NOW)
        self.assertEqual(
            d.explanation["components"]["history"]["excluded_reason"],
            "INSUFFICIENT_HISTORY_TRADES")

    def test_low_confidence_downgrades_to_hold(self):
        agg = SignalAggregator(AggregationConfig(min_confidence=99.0))
        d = agg.aggregate(inputs(), as_of=NOW)
        self.assertIs(d.action, AggregatedAction.HOLD)
        self.assertIn("LOW_CONFIDENCE", d.reason_codes)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            inputs(technical=1.5)
        with self.assertRaises(ValueError):
            inputs(news=float("nan"))
        with self.assertRaises(ValueError):
            inputs(timestamp=datetime(2026, 10, 5))
        with self.assertRaises(ValueError):
            AggregationConfig(weights={"technical": 1.0})

    def test_to_signal_mapping(self):
        buy = self.aggregator.aggregate(inputs(), as_of=NOW)
        signal = buy.to_signal(entry_price=100.0, stop_loss=98.0)
        self.assertIs(signal.side, SignalSide.BUY)
        self.assertEqual(signal.signal_id, buy.decision_id)
        skip = self.aggregator.aggregate(inputs(technical=None), as_of=NOW)
        self.assertIs(skip.to_signal(entry_price=100.0).side, SignalSide.HOLD)


if __name__ == "__main__":
    unittest.main()
