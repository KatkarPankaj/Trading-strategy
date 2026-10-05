from datetime import datetime, time, timezone
import unittest

from stockmarket.config import TradingConfig
from stockmarket.core import MarketSession


class MarketSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = MarketSession.from_config(TradingConfig())

    def ist(self, hour, minute):
        return datetime(
            2026, 10, 5, hour, minute, tzinfo=timezone.utc
        )

    def test_default_session_matches_nse_boundaries(self):
        self.assertEqual(self.session.timezone, "Asia/Kolkata")
        self.assertEqual(self.session.market_open, time(9, 15))
        self.assertEqual(self.session.opening_range_end, time(9, 30))
        self.assertEqual(self.session.entry_cutoff, time(13, 30))
        self.assertEqual(self.session.square_off, time(15, 15))
        self.assertEqual(self.session.market_close, time(15, 30))

    def test_before_open_and_opening_range_boundaries(self):
        self.assertTrue(self.session.is_before_open(self.ist(3, 44)))
        self.assertTrue(self.session.is_opening_range(self.ist(3, 45)))
        self.assertTrue(self.session.is_opening_range(self.ist(4, 0)))
        self.assertFalse(self.session.is_opening_range(self.ist(4, 1)))

    def test_entry_window_and_open_market_state(self):
        self.assertFalse(self.session.is_entry_allowed(self.ist(3, 44)))
        self.assertTrue(self.session.is_entry_allowed(self.ist(4, 0)))
        self.assertTrue(self.session.is_entry_allowed(self.ist(8, 0)))
        self.assertFalse(self.session.is_entry_allowed(self.ist(8, 1)))
        self.assertTrue(self.session.is_market_open(self.ist(3, 45)))

    def test_square_off_and_after_close(self):
        self.assertFalse(self.session.is_square_off(self.ist(9, 44)))
        self.assertTrue(self.session.is_square_off(self.ist(9, 45)))
        self.assertFalse(self.session.is_market_closed(self.ist(10, 0)))
        self.assertTrue(self.session.is_market_closed(self.ist(10, 1)))

    def test_simple_dashboard_can_preserve_entry_from_open(self):
        simple_session = MarketSession.from_config(
            TradingConfig(), entry_starts_at_open=True
        )
        self.assertTrue(simple_session.is_entry_allowed(self.ist(3, 45)))

    def test_rejects_naive_datetimes(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.session.is_entry_allowed(datetime(2026, 10, 5, 9, 30))

    def test_custom_timezone_conversion_uses_same_exchange_local_boundaries(self):
        utc_session = MarketSession.from_config(
            TradingConfig(market_timezone="UTC")
        )
        self.assertTrue(
            utc_session.is_entry_allowed(
                datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
            )
        )


if __name__ == "__main__":
    unittest.main()
