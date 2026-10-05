from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from stockmarket.news import (
    MarketImpact,
    NewsAnalysis,
    NewsEvent,
    NewsEventType,
    NewsProvider,
    NewsQuery,
    NewsSentiment,
)


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


class InMemoryNewsProvider:
    def __init__(self, events):
        self.events = tuple(events)

    def get_news(self, query):
        matches = [
            event for event in self.events
            if (not query.symbols or event.symbol in query.symbols)
            and (query.affected_market is None or event.affected_market == query.affected_market)
            and (query.start_time is None or event.timestamp >= query.start_time)
            and (query.end_time is None or event.timestamp <= query.end_time)
        ]
        return tuple(sorted(matches, key=lambda event: event.timestamp, reverse=True)[:query.limit])


class NewsEventTests(unittest.TestCase):
    def make_event(self, **overrides):
        values = {
            "timestamp": NOW,
            "source": "exchange announcement",
            "headline": "Company reports quarterly results",
            "event_type": NewsEventType.EARNINGS,
            "sentiment": NewsSentiment.POSITIVE,
            "sentiment_confidence": 0.85,
            "market_impact": MarketImpact.HIGH,
            "relevance": 0.95,
            "symbol": "RELIANCE.NS",
            "content": "Revenue exceeded estimates.",
            "reference": "https://example.invalid/announcement/1",
            "affected_sector": "Energy",
            "affected_market": "IN",
            "provider_event_id": "vendor-123",
        }
        values.update(overrides)
        return NewsEvent(**values)

    def test_event_has_generated_stable_id_and_normalized_fields(self):
        event = self.make_event()
        self.assertIsNotNone(event.event_id)
        self.assertEqual(event.event_type, NewsEventType.EARNINGS)
        self.assertEqual(event.symbol, "RELIANCE.NS")

    def test_supported_event_categories_cover_required_types(self):
        expected = {
            "earnings", "guidance", "acquisition", "merger", "contract",
            "product_launch", "regulatory", "lawsuit", "management_change",
            "analyst_action", "macroeconomic_event", "geopolitical_event",
            "sector_event",
        }
        self.assertTrue(expected.issubset(
            {item.value for item in NewsEventType}))

    def test_rejects_naive_time_invalid_confidence_and_wrong_enum(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.make_event(timestamp=datetime(2026, 10, 5, 10, 0))
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            self.make_event(sentiment_confidence=1.1)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            self.make_event(relevance=-0.1)
        with self.assertRaises(TypeError):
            self.make_event(event_type="earnings")

    def test_global_event_can_omit_symbol_and_optional_context(self):
        event = self.make_event(
            event_type=NewsEventType.MACROECONOMIC_EVENT,
            symbol=None,
            content=None,
            reference=None,
            affected_sector=None,
            affected_market=None,
            provider_event_id=None,
        )
        self.assertIsNone(event.symbol)
        self.assertIsNone(event.affected_market)


class NewsQueryAndProviderTests(unittest.TestCase):
    def test_query_validates_time_range_and_limit(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            NewsQuery(start_time=datetime(2026, 10, 5, 10, 0))
        with self.assertRaisesRegex(ValueError, "end_time"):
            NewsQuery(start_time=NOW, end_time=NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "limit"):
            NewsQuery(limit=0)

    def test_provider_protocol_and_query_contract(self):
        event_old = NewsEvent(
            timestamp=NOW - timedelta(hours=1),
            source="wire",
            headline="Older earnings note",
            event_type=NewsEventType.EARNINGS,
            sentiment=NewsSentiment.NEUTRAL,
            sentiment_confidence=0.5,
            market_impact=MarketImpact.MEDIUM,
            relevance=0.7,
            symbol="RELIANCE.NS",
            affected_market="IN",
        )
        event_new = NewsEvent(
            timestamp=NOW,
            source="wire",
            headline="New contract announcement",
            event_type=NewsEventType.CONTRACT,
            sentiment=NewsSentiment.POSITIVE,
            sentiment_confidence=0.9,
            market_impact=MarketImpact.HIGH,
            relevance=0.9,
            symbol="TCS.NS",
            affected_market="IN",
        )
        provider = InMemoryNewsProvider([event_old, event_new])
        self.assertIsInstance(provider, NewsProvider)
        results = provider.get_news(
            NewsQuery(symbols=("RELIANCE.NS",), affected_market="IN")
        )
        self.assertEqual([item.event_id for item in results],
                         [event_old.event_id])


class NewsAnalysisTests(unittest.TestCase):
    def test_parses_structured_analysis_without_execution_fields(self):
        event_id = uuid4()
        analysis = NewsAnalysis.from_mapping(
            {
                "summary": "Quarterly revenue rose.",
                "event_type": "earnings",
                "sentiment": "positive",
                "sentiment_confidence": 0.8,
                "market_impact": "medium",
                "relevance": 0.9,
                "key_points": ["Revenue increased"],
                "risks": ["Guidance remains uncertain"],
            },
            event_id=event_id,
            analyzed_at=NOW,
        )
        self.assertEqual(analysis.event_id, event_id)
        self.assertEqual(analysis.key_points, ("Revenue increased",))
        self.assertFalse(hasattr(analysis, "order"))
        self.assertFalse(hasattr(analysis, "action"))

    def test_rejects_unstructured_or_order_like_ai_output(self):
        base = {
            "summary": "Potentially positive result.",
            "event_type": "earnings",
            "sentiment": "positive",
            "sentiment_confidence": 0.8,
            "market_impact": "medium",
            "relevance": 0.9,
        }
        with self.assertRaisesRegex(ValueError, "execution commands"):
            NewsAnalysis.from_mapping(
                {**base, "action": "BUY"}, event_id=uuid4(), analyzed_at=NOW
            )
        with self.assertRaisesRegex(ValueError, "missing fields"):
            NewsAnalysis.from_mapping(
                {"summary": "missing categories"},
                event_id=uuid4(),
                analyzed_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
