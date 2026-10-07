from datetime import date, datetime, time, timezone
import unittest

from stockmarket.core.markets import MarketDefinition, SessionPhase, default_markets
from stockmarket.core.trading_calendar import TradingCalendar


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = TradingCalendar(
            timezone="America/New_York",
            open_time=time(9, 30),
            close_time=time(16, 0),
            holidays=frozenset({date(2026, 7, 3)}),
            early_closes={date(2026, 11, 27): time(13, 0)},
            trading_pauses=((time(12, 0), time(12, 30)),),
            covered_years=frozenset({2026}),
        )

    def ny(self, month, day, hour, minute):
        return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)

    def test_open_and_close_are_timezone_aware_and_half_open(self):
        self.assertFalse(self.calendar.is_open(self.ny(10, 5, 13, 29)))
        self.assertTrue(self.calendar.is_open(self.ny(10, 5, 13, 30)))
        self.assertTrue(self.calendar.is_open(self.ny(10, 5, 19, 59)))
        self.assertFalse(self.calendar.is_open(self.ny(10, 5, 20, 0)))

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.calendar.is_open(datetime(2026, 10, 5, 9, 30))

    def test_holidays_closed_weekends_and_uncovered_year_fail_closed(self):
        self.assertFalse(self.calendar.is_trading_day(date(2026, 7, 3)))
        self.assertFalse(self.calendar.is_trading_day(date(2026, 10, 3)))
        self.assertFalse(self.calendar.is_trading_day(date(2027, 10, 4)))
        self.assertFalse(self.calendar.is_open(self.ny(7, 3, 14, 0)))

    def test_early_close_and_intraday_pause_are_respected(self):
        self.assertTrue(self.calendar.is_open(self.ny(11, 27, 17, 59)))
        self.assertFalse(self.calendar.is_open(self.ny(11, 27, 18, 0)))
        self.assertFalse(self.calendar.is_open(self.ny(10, 5, 16, 0)))
        self.assertFalse(self.calendar.is_open(self.ny(10, 5, 16, 29)))
        self.assertTrue(self.calendar.is_open(self.ny(10, 5, 16, 30)))

    def test_rejects_malformed_pauses_and_early_closes(self):
        with self.assertRaisesRegex(ValueError, "ordered, non-overlapping"):
            TradingCalendar(
                "UTC", time(9), time(17),
                trading_pauses=((time(12), time(13)), (time(12, 30), time(14))),
            )
        with self.assertRaisesRegex(ValueError, "early close"):
            TradingCalendar(
                "UTC", time(9), time(17),
                early_closes={date(2026, 1, 1): time(8, 0)},
            )


class MarketPhaseCalendarTests(unittest.TestCase):
    def test_market_phase_respects_early_close_and_unknown_calendar_year(self):
        market = default_markets().get("US")
        early_close = datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
        after_early_close = datetime(
            2026, 11, 27, 18, 0, tzinfo=timezone.utc)
        unknown_year = datetime(2027, 10, 4, 15, 0, tzinfo=timezone.utc)

        self.assertEqual(market.phase(early_close), SessionPhase.REGULAR)
        self.assertEqual(market.phase(after_early_close), SessionPhase.POST_MARKET)
        self.assertEqual(market.phase(unknown_year), SessionPhase.CLOSED)

    def test_market_phase_closes_during_calendar_pause(self):
        calendar = TradingCalendar(
            timezone="UTC",
            open_time=time(9),
            close_time=time(17),
            covered_years=frozenset({2026}),
            trading_pauses=((time(12), time(13)),),
        )
        market = MarketDefinition(
            code="TEST",
            name="Test exchange",
            mics=("XTST",),
            currency="USD",
            calendar=calendar,
        )

        self.assertEqual(
            market.phase(datetime(2026, 10, 5, 11, 59, tzinfo=timezone.utc)),
            SessionPhase.REGULAR,
        )
        self.assertEqual(
            market.phase(datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)),
            SessionPhase.CLOSED,
        )
        self.assertEqual(
            market.phase(datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)),
            SessionPhase.REGULAR,
        )


if __name__ == "__main__":
    unittest.main()
