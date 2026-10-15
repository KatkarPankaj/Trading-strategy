"""Offline cadence regression checks using only deterministic paper fixtures."""

from datetime import timedelta
import unittest
from unittest.mock import patch

import pandas as pd

from stockmarket.core import OrbVwapConfig, OrbVwapStrategy, SignalSide
from test_deterministic_strategies import make_bars, make_instrument, make_session


class PaperCadenceAuditTests(unittest.TestCase):
    def test_ten_minute_phases_use_completed_five_minute_bars_without_lookahead(self):
        fixture = make_bars()
        future_times = pd.date_range(
            fixture.index[-1] + timedelta(minutes=5),
            periods=8,
            freq="5min",
        )
        neutral = pd.DataFrame(
            {
                "open": 102.0,
                "high": 103.0,
                "low": 101.0,
                "close": 102.0,
                "volume": 100.0,
            },
            index=future_times,
        )
        bars = pd.concat((fixture, neutral))
        strategy = OrbVwapStrategy(OrbVwapConfig(interval="5m"))
        instrument, session = make_instrument(), make_session()
        completed_bar_decisions = {
            stamp.to_pydatetime() + timedelta(minutes=5) for stamp in bars.index
        }
        phase_results = {}

        for phase_minutes in (0, 5):
            phase = timedelta(minutes=phase_minutes)
            scan_anchor = bars.index[0] + phase
            sampled = []
            while scan_anchor <= bars.index[-1]:
                decision_at = scan_anchor.to_pydatetime() + timedelta(minutes=5)
                available = bars.loc[bars.index <= scan_anchor]
                self.assertFalse(available.empty)
                self.assertIn(decision_at, completed_bar_decisions)
                self.assertLessEqual(
                    available.index[-1].to_pydatetime() + timedelta(minutes=5),
                    decision_at,
                )
                signal = strategy.evaluate(
                    instrument, available, session, as_of=decision_at)
                sampled.append((scan_anchor.strftime("%H:%M"), signal))
                scan_anchor += timedelta(minutes=10)
            phase_results[phase_minutes] = sampled

        self.assertEqual(
            [anchor for anchor, _ in phase_results[0]],
            ["09:30", "09:40", "09:50", "10:00", "10:10", "10:20", "10:30"],
        )
        self.assertEqual(
            [anchor for anchor, _ in phase_results[5]],
            ["09:35", "09:45", "09:55", "10:05", "10:15", "10:25"],
        )
        on_phase = [
            (anchor, signal) for anchor, signal in phase_results[0]
            if signal.side is not SignalSide.HOLD
        ]
        off_phase = [
            (anchor, signal) for anchor, signal in phase_results[5]
            if signal.side is not SignalSide.HOLD
        ]
        self.assertEqual(
            [(anchor, signal.side, signal.timestamp, signal.entry_price, signal.reasons)
             for anchor, signal in on_phase],
            [("09:50", SignalSide.BUY, bars.index[4].to_pydatetime(), 103.0,
              ("ORB_BREAKOUT_ABOVE", "CLOSE_ABOVE_VWAP", "VOLUME_SPIKE"))],
        )
        self.assertEqual(off_phase, [])

    def test_end_to_end_paper_cycle_counts_local_quote_and_bar_reads(self):
        from test_paper_end_to_end import PaperEndToEndTests

        fixture = PaperEndToEndTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context = fixture.context(auto_loop=True)

        with (
            patch.object(fixture.data, "get_quote", wraps=fixture.data.get_quote) as quotes,
            patch.object(fixture.data, "get_ohlcv", wraps=fixture.data.get_ohlcv) as bars,
        ):
            result = context.paper_loop.tick()

        self.assertEqual((result["action"], result.get("status")), ("CYCLE", "COMPLETE"), result)
        quote_calls = quotes.call_count
        bar_calls = bars.call_count
        self.assertEqual(quote_calls, 3)
        self.assertEqual(bar_calls, 3)
        print(
            f"offline E2E cycle data-adapter calls: quote={quote_calls}, bars={bar_calls}; "
            "mock fixture calls only (not HTTP attempts or provider credits)"
        )


if __name__ == "__main__":
    unittest.main()
