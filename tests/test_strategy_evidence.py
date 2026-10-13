from datetime import datetime, timedelta, timezone
import unittest

from stockmarket.core.backtesting.evidence import PerformanceObservation, summarize_evidence


class StrategyEvidenceTests(unittest.TestCase):
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)

    def observations(self, returns):
        return [PerformanceObservation(
            str(i), self.now - timedelta(days=100 - i),
            self.now - timedelta(days=100 - i) + timedelta(minutes=5),
            self.now - timedelta(days=100 - i) + timedelta(minutes=10),
            1000, r * 1000 + 2, 2, r * 1000) for i, r in enumerate(returns)]

    def test_costs_uncertainty_drawdown_profit_factor_and_stability_are_explicit(self):
        values = [.02, -.005] * 30
        result = summarize_evidence(self.observations(values), as_of=self.now, comparisons=8)
        self.assertEqual(result["trade_count"], 60)
        self.assertAlmostEqual(result["net_return_expectancy"], .0075)
        self.assertAlmostEqual(result["profit_factor"], 4)
        self.assertAlmostEqual(result["gross_pnl"] - result["fees"], result["net_pnl"])
        self.assertGreater(result["closed_return_drawdown"], 0)
        self.assertEqual(len(result["chronological_windows"]), 3)
        single = summarize_evidence(self.observations(values), as_of=self.now)
        self.assertLess(result["score"], single["score"])
        volatile = summarize_evidence(self.observations([.02] * 20 + [.01] * 20 + [-.0075] * 20),
                                      as_of=self.now)
        stable = summarize_evidence(self.observations([.0075] * 60), as_of=self.now)
        self.assertLess(volatile["score"], stable["score"])

    def test_same_day_samples_and_small_samples_are_not_qualified(self):
        small = summarize_evidence(self.observations([.5] * 29), as_of=self.now)
        self.assertIsNone(small["score"])
        day = self.now - timedelta(days=1)
        same_day = [PerformanceObservation(str(i), day + timedelta(minutes=2 * i),
                    day + timedelta(minutes=2 * i + 1), day + timedelta(minutes=2 * i + 1),
                    1000, 12, 2, 10) for i in range(30)]
        self.assertIsNone(summarize_evidence(same_day, as_of=self.now)["score"])

    def test_duplicate_overlap_future_and_invalid_cost_accounting_fail_explicitly(self):
        observations = self.observations([.01] * 30)
        for rows, at in ((observations + observations[:1], self.now),
                         (observations, observations[0].closed_at),
                         (observations, self.now.replace(tzinfo=None))):
            with self.assertRaises(ValueError):
                summarize_evidence(rows, as_of=at)
        a = observations[0]
        overlap = PerformanceObservation("overlap", a.opened_at, a.closed_at, a.known_at, 1000, 12, 2, 10)
        with self.assertRaisesRegex(ValueError, "overlapping"):
            summarize_evidence([a, overlap], as_of=self.now)
        with self.assertRaises(ValueError):
            PerformanceObservation("invalid", a.opened_at, a.closed_at, a.known_at, 1000, 10, 2, 10)
