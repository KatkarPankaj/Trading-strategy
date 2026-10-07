from datetime import datetime, timedelta, timezone
import unittest

import pandas as pd
from types import SimpleNamespace

from stockmarket.core.brokers import MarketStatus
from stockmarket.core.data import (
    DataPolicy,
    DataQualityError,
    MarketDataProvider,
    MockProvider,
    Quote,
    ResilientProvider,
    YahooProvider,
    create_market_data_provider,
    quote_source,
    validate_bars,
    validate_quote,
)
from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import default_markets
from stockmarket.core.models import (
    AssetClass,
    OrderSide,
    OrderType,
    RiskDecision,
    RiskDecisionStatus,
)
from stockmarket.core.resilience import RetryPolicy
from stockmarket.core.trading_service import OrderTicket, TradingService


NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def make_bars(index):
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [10.0] * len(index),
        },
        index=index,
    )


class FakeProvider(MarketDataProvider):
    name = "fake"
    research_only = True

    def __init__(self):
        self.quote = Quote("XNAS:AAPL", 100.0, NOW - timedelta(seconds=10), self.name)
        self.bars = make_bars(pd.date_range(NOW - timedelta(minutes=2), periods=2, freq="min"))
        self.quote_calls = 0
        self.bar_calls = 0

    def get_instrument(self, symbol, market=None):
        return make_instrument()

    def get_quote(self, instrument):
        self.quote_calls += 1
        return self.quote

    def get_ohlcv(self, instrument, interval, start, end):
        self.bar_calls += 1
        return self.bars

    def get_market_status(self, market):
        return MarketStatus(market, True, NOW, "REGULAR")


class MarketDataProviderTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()
        self.raw_provider = FakeProvider()
        self.provider = ResilientProvider(
            self.raw_provider,
            policy=DataPolicy(retry=RetryPolicy(max_attempts=1)),
            clock=lambda: NOW,
            sleep=lambda _: None,
        )
        self.addCleanup(self.provider.close)

    def test_valid_quote_passes_and_records_its_identity(self):
        quote = self.provider.get_quote(self.instrument)
        self.assertEqual(quote.instrument_id, self.instrument.instrument_id)
        self.assertEqual(quote.provider, self.raw_provider.name)
        self.assertEqual(self.raw_provider.quote_calls, 1)

    def test_quote_for_wrong_instrument_is_rejected(self):
        self.raw_provider.quote = Quote(
            "XNAS:MSFT", 100.0, NOW - timedelta(seconds=10), self.raw_provider.name)
        with self.assertRaises(DataQualityError) as caught:
            self.provider.get_quote(self.instrument)
        self.assertEqual(caught.exception.issues, ("INSTRUMENT_MISMATCH",))

    def test_quote_with_wrong_provider_is_rejected(self):
        self.raw_provider.quote = Quote(
            self.instrument.instrument_id, 100.0, NOW - timedelta(seconds=10), "other")
        with self.assertRaises(DataQualityError) as caught:
            self.provider.get_quote(self.instrument)
        self.assertEqual(caught.exception.issues, ("PROVIDER_MISMATCH",))

    def test_quote_price_off_instrument_tick_is_rejected(self):
        self.raw_provider.quote = Quote(
            self.instrument.instrument_id, 100.001, NOW - timedelta(seconds=10),
            self.raw_provider.name)
        with self.assertRaises(DataQualityError) as caught:
            self.provider.get_quote(self.instrument)
        self.assertEqual(caught.exception.issues, ("PRICE_OFF_TICK",))

    def test_malformed_quote_fields_are_reported_without_type_errors(self):
        quote = Quote(
            self.instrument.instrument_id, 100.0, NOW, self.raw_provider.name,
            bid="bad", ask="also bad", volume=-1)
        report = validate_quote(quote, NOW, timedelta(minutes=1))
        self.assertFalse(report.ok)
        self.assertIn("INVALID_BID", report.issues)
        self.assertIn("INVALID_ASK", report.issues)
        self.assertIn("INVALID_VOLUME", report.issues)

    def test_invalid_quote_object_is_reported(self):
        self.assertEqual(
            validate_quote(object(), NOW, timedelta(minutes=1)).issues,
            ("INVALID_QUOTE",))

    def test_invalid_data_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            DataPolicy(timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "max_quote_age"):
            DataPolicy(max_quote_age=timedelta(0))

    def test_bar_request_requires_aware_ordered_bounds_before_provider_call(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.provider.get_ohlcv(
                self.instrument, "1m", datetime(2026, 10, 5), NOW)
        with self.assertRaisesRegex(ValueError, "start before end"):
            self.provider.get_ohlcv(self.instrument, "1m", NOW, NOW)
        self.assertEqual(self.raw_provider.bar_calls, 0)

    def test_bar_response_outside_requested_range_is_rejected(self):
        start, end = NOW - timedelta(minutes=1), NOW
        self.raw_provider.bars = make_bars(
            pd.date_range(NOW - timedelta(minutes=3), periods=2, freq="min"))
        with self.assertRaises(DataQualityError) as caught:
            self.provider.get_ohlcv(self.instrument, "1m", start, end)
        self.assertIn("OUT_OF_REQUEST_RANGE", caught.exception.issues)

    def test_supported_provider_factory_is_research_only_and_rejects_unknowns(self):
        instruments = {self.instrument.instrument_id: self.instrument}
        markets = default_markets()
        mock = create_market_data_provider("mock", instruments, markets=markets)
        yahoo = create_market_data_provider("yahoo", instruments, markets=markets)
        self.assertIsInstance(mock, MockProvider)
        self.assertIsInstance(yahoo, YahooProvider)
        self.assertTrue(mock.research_only)
        self.assertTrue(yahoo.research_only)
        with self.assertRaisesRegex(ValueError, "unsupported DATA_PROVIDER"):
            create_market_data_provider("unknown", instruments, markets=markets)

    def test_yahoo_normalization_preserves_duplicate_and_unsorted_timestamps(self):
        index = pd.DatetimeIndex(
            [NOW - timedelta(minutes=1), NOW - timedelta(minutes=2),
             NOW - timedelta(minutes=1)])
        normalized = YahooProvider._normalize(make_bars(index), self.instrument)
        report = validate_bars(
            normalized, "1m", timezone=self.instrument.timezone)
        self.assertIn("DUPLICATE_TIMESTAMPS", report.issues)
        self.assertIn("UNSORTED_TIMESTAMPS", report.issues)

    def test_quote_source_logs_provider_failures_and_fails_closed(self):
        class Logger:
            def __init__(self):
                self.events = []

            def error(self, message, **fields):
                self.events.append((message, fields))

        self.raw_provider.quote = Quote(
            "XNAS:MSFT", 100.0, NOW - timedelta(seconds=10), self.raw_provider.name)
        logger = Logger()
        source = quote_source(
            self.provider, {self.instrument.instrument_id: self.instrument}, logger=logger)
        self.assertIsNone(source(self.instrument.instrument_id))
        self.assertEqual(len(logger.events), 1)
        self.assertEqual(logger.events[0][1]["error_type"], "DataQualityError")

    def test_one_provider_snapshot_is_reused_for_risk_context(self):
        quote_calls = []
        decision_time = NOW
        portfolio = SimpleNamespace(
            positions=lambda: {},
            cash={"USD": 1000.0},
            fills=[],
            risk_state=lambda *args, **kwargs: None,
        )
        risk_engine = SimpleNamespace()

        def evaluate_proposal(*args, **kwargs):
            risk_engine.context = kwargs["context"]
            return RiskDecision(RiskDecisionStatus.REJECTED, decision_time, "test rejection")

        risk_engine.evaluate_proposal = evaluate_proposal
        order_manager = SimpleNamespace(
            orders=lambda: [],
            submit=lambda request, decision: SimpleNamespace(
                order=SimpleNamespace(is_terminal=True, broker_order_id=None,
                                      client_order_id=request.client_order_id),
                duplicate=False,
            ),
        )

        def get_quote(instrument_id):
            quote_calls.append(instrument_id)
            return 99.99, NOW - timedelta(seconds=5)

        service = TradingService(
            mode=TradingMode.PAPER,
            risk_engine=risk_engine,
            order_manager=order_manager,
            portfolio=portfolio,
            instruments={self.instrument.instrument_id: self.instrument},
            quotes=get_quote,
            clock=lambda: decision_time,
        )
        service.submit(OrderTicket(
            instrument_id=self.instrument.instrument_id,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            strategy="test",
        ))
        self.assertEqual(quote_calls, [self.instrument.instrument_id])
        self.assertEqual(risk_engine.context.reference_price, 99.99)
        self.assertEqual(risk_engine.context.market_data_timestamp, NOW - timedelta(seconds=5))

if __name__ == "__main__":
    unittest.main()
