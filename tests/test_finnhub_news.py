from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from stockmarket.news import FinnhubNewsProvider, FinnhubNewsUnavailable, NewsQuery


AS_OF = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def event(event_id, timestamp, **overrides):
    values = {
        "id": event_id,
        "datetime": int(timestamp.timestamp()),
        "headline": f"Company update {event_id}",
        "source": "Example News",
        "summary": "A bounded research summary.",
        "url": "https://example.invalid/news",
        "related": "AAPL",
    }
    values.update(overrides)
    return values


class FinnhubNewsProviderTests(unittest.TestCase):
    def query(self, **overrides):
        values = {
            "symbols": ("AAPL",),
            "affected_market": "US",
            "start_time": AS_OF - timedelta(hours=1),
            "end_time": AS_OF,
            "limit": 5,
        }
        values.update(overrides)
        return NewsQuery(**values)

    def response(self, payload):
        return BytesIO(json.dumps(payload).encode("utf-8"))

    @patch("stockmarket.news.finnhub.urlopen")
    def test_auth_header_mapping_and_timestamp_filters(self, urlopen):
        urlopen.return_value.__enter__.return_value = self.response([
            event(1, AS_OF - timedelta(minutes=2)),
            event(2, AS_OF + timedelta(seconds=1)),
            event(3, AS_OF - timedelta(minutes=1), related="MSFT"),
        ])
        provider = FinnhubNewsProvider(
            "test-key", symbol_map={"IN:RELIANCE": "RELIANCE.NS"})

        events = provider.get_news(self.query())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].provider_event_id, "1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-finnhub-token"), "test-key")
        self.assertNotIn("test-key", request.full_url)

    @patch("stockmarket.news.finnhub.urlopen")
    def test_non_us_symbol_requires_explicit_mapping(self, urlopen):
        provider = FinnhubNewsProvider("test-key")

        with self.assertRaisesRegex(FinnhubNewsUnavailable, "explicit Finnhub"):
            provider.get_news(self.query(
                symbols=("RELIANCE",), affected_market="IN"))
        urlopen.assert_not_called()

    @patch("stockmarket.news.finnhub.urlopen")
    def test_malformed_events_and_provider_auth_failures_are_surfaced(self, urlopen):
        provider = FinnhubNewsProvider("test-key")
        urlopen.return_value.__enter__.return_value = self.response([
            event(1, AS_OF, headline=" "),
        ])
        with self.assertRaisesRegex(FinnhubNewsUnavailable, "missing a valid"):
            provider.get_news(self.query())

        urlopen.side_effect = HTTPError(
            "https://finnhub.io", 401, "unauthorized", {}, None)
        with self.assertRaisesRegex(FinnhubNewsUnavailable, "authentication"):
            provider.get_news(self.query())


if __name__ == "__main__":
    unittest.main()
