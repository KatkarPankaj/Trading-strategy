from datetime import datetime, timedelta, timezone
import json
import unittest

from stockmarket.core.ai import AIAnalyst
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.research import NewsEvidenceProducer, NewsResearchUnavailable
from stockmarket.news import (
    MarketImpact,
    NewsEvent,
    NewsEventType,
    NewsQuery,
    NewsSentiment,
)


AS_OF = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def make_event(**overrides):
    values = {
        "timestamp": AS_OF - timedelta(minutes=2),
        "source": "exchange",
        "headline": "Company announces a major contract",
        "event_type": NewsEventType.CONTRACT,
        "sentiment": NewsSentiment.UNKNOWN,
        "sentiment_confidence": 0.0,
        "market_impact": MarketImpact.HIGH,
        "relevance": 0.9,
        "symbol": "AAPL",
        "affected_market": "US",
    }
    values.update(overrides)
    return NewsEvent(**values)


def ai_response(**overrides):
    payload = {
        "summary": "A material contract announcement may support the issuer.",
        "event_type": "contract",
        "sentiment": "positive",
        "sentiment_confidence": 0.9,
        "market_impact": "high",
        "relevance": 0.8,
        "key_points": ["Contract announced"],
        "risks": ["Financial contribution is not quantified."],
    }
    payload.update(overrides)
    return json.dumps(payload)


class StaticNewsProvider:
    def __init__(self, events=None, error=None):
        self.events = events or ()
        self.error = error
        self.queries = []

    def get_news(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.events


class StaticAIProvider:
    name = "test-ai"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self.response


class NewsEvidenceProducerTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()

    def producer(self, events=None, response=None, error=None):
        news_provider = StaticNewsProvider(events, error)
        ai_provider = StaticAIProvider(response or ai_response())
        producer = NewsEvidenceProducer(
            news_provider,
            AIAnalyst(ai_provider, clock=lambda: AS_OF),
            max_age=timedelta(hours=1),
            limit=10,
        )
        return producer, news_provider, ai_provider

    def test_creates_bounded_timestamped_evidence_from_matched_news(self):
        event = make_event()
        producer, news_provider, ai_provider = self.producer([event])

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(len(news_provider.queries), 1)
        query = news_provider.queries[0]
        self.assertIsInstance(query, NewsQuery)
        self.assertEqual(query.symbols, ("AAPL",))
        self.assertEqual(query.affected_market, "US")
        self.assertEqual(query.start_time, AS_OF - timedelta(hours=1))
        self.assertEqual(collection.event_count, 1)
        self.assertEqual(collection.analyzed_count, 1)
        self.assertEqual(len(collection.evidence), 1)
        evidence = collection.evidence[0]
        self.assertEqual(evidence.instrument_id, self.instrument.instrument_id)
        self.assertEqual(evidence.component, "news")
        self.assertEqual(evidence.observed_at, event.timestamp)
        self.assertEqual(evidence.max_age, timedelta(hours=1))
        self.assertEqual(evidence.source[:8], "ai_news:")
        self.assertAlmostEqual(evidence.score, 0.72)
        self.assertEqual(ai_provider.calls, 1)
        self.assertGreaterEqual(evidence.score, -1.0)
        self.assertLessEqual(evidence.score, 1.0)

    def test_non_directional_news_is_reported_but_not_converted_to_zero_score(self):
        producer, _, _ = self.producer(
            [make_event()],
            ai_response(sentiment="unknown"),
        )

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(collection.evidence, ())
        self.assertEqual(collection.analyzed_count, 0)
        self.assertIn("NO_DIRECTIONAL_SCORE", collection.warnings[0])

    def test_no_matching_events_is_explicit(self):
        producer, _, _ = self.producer()

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(collection.evidence, ())
        self.assertEqual(collection.event_count, 0)
        self.assertEqual(collection.warnings, ("NO_MATCHING_NEWS_EVENTS",))

    def test_provider_failure_is_surfaced(self):
        producer, _, _ = self.producer(error=RuntimeError("offline"))

        with self.assertRaisesRegex(NewsResearchUnavailable, "RuntimeError"):
            producer.collect(self.instrument, as_of=AS_OF)

    def test_mismatched_future_stale_and_duplicate_events_are_rejected(self):
        invalid_sets = (
            ([make_event(symbol="MSFT")], "instrument-mismatched"),
            ([make_event(timestamp=AS_OF + timedelta(seconds=1))], "future or stale"),
            ([make_event(timestamp=AS_OF - timedelta(hours=2))], "future or stale"),
        )
        duplicate = make_event()
        invalid_sets += (([duplicate, duplicate], "duplicate event"),)

        for events, message in invalid_sets:
            with self.subTest(message=message):
                producer, _, ai_provider = self.producer(events)
                with self.assertRaisesRegex(NewsResearchUnavailable, message):
                    producer.collect(self.instrument, as_of=AS_OF)
                self.assertEqual(ai_provider.calls, 0)

    def test_ai_schema_failure_becomes_visible_warning_without_evidence(self):
        producer, _, _ = self.producer([make_event()], '{"action":"BUY"}')

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(collection.evidence, ())
        self.assertIn("SCHEMA_VALIDATION_FAILED", collection.warnings[0])


if __name__ == "__main__":
    unittest.main()
