from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

import test_paper_end_to_end as paper_fixture
from stockmarket.core.outcome_feedback import OutcomeFeedbackService
from stockmarket.core.strategy_ranking import StrategyRankingService
from stockmarket.core.strategies import OrbVwapStrategy
from stockmarket.core.strategies.base import strategy_parameters_hash
from stockmarket.core.strategies.trend import TrendStrategy


class StrategyRankingTests(unittest.TestCase):
    def setUp(self):
        self.store = paper_fixture.make_store()
        self.addCleanup(self.store.db.close)
        self.now = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
        self.strategies = {"orb_vwap": OrbVwapStrategy(), "sma_trend": TrendStrategy()}
        self.feedback = OutcomeFeedbackService(self.store, clock=lambda: self.now)
        self.ranking = StrategyRankingService(self.store, self.feedback, clock=lambda: self.now)

    def outcome(self, strategy, index, pnl, *, regime="UNKNOWN", known_at=None):
        self.store.paper_outcomes.save({
            "outcome_id": f"{strategy}-{index}", "instrument_id": "XNAS:AAPL", "market": "US",
            "currency": "USD", "opened_at": (self.now - timedelta(days=3)).isoformat(),
            "closed_at": (self.now - timedelta(days=2)).isoformat(), "fill_ids": [f"fill-{strategy}-{index}"],
            "attribution_status": "COMPLETE",
            "attribution": {"strategy_id": strategy, "strategy_version": "1.0.0",
                            "parameters_hash": strategy_parameters_hash(self.strategies[strategy]),
                            "timeframe": "5m", "market_regime": regime},
            "gross_pnl": pnl + 2, "fees": 2, "net_pnl": pnl, "entry_notional": 1000,
        }, known_at or (self.now - timedelta(days=1)))

    def context(self, as_of=None):
        return SimpleNamespace(
            outcome_feedback=self.ranking.evidence(as_of or self.now, "US", "USD", self.strategies),
            instrument_id="XNAS:AAPL", snapshot_id="fixture-snapshot", as_of=as_of or self.now)

    def test_qualified_outcomes_change_actual_order_and_record_reasons(self):
        for i in range(30):
            self.outcome("orb_vwap", i, -10)
            self.outcome("sma_trend", i, 10)
        record = self.ranking.rank(["orb_vwap", "sma_trend"], self.context(), assessment_id="qualified")
        self.assertEqual(record["ranked_order"], ["sma_trend", "orb_vwap"])
        self.assertTrue(record["changed"])
        self.assertEqual(record["evidence"][0]["sample_size"], 30)
        self.assertEqual(record["evidence"][1]["source"], "PAPER_OUTCOMES")
        self.assertEqual(self.store.strategy_lab.rankings()[0], record)
        self.assertEqual(self.store.orders.all(), [])

    def test_small_unknown_regime_or_future_samples_do_not_improve_preference(self):
        self.outcome("orb_vwap", 0, -100)
        for i in range(30):
            self.outcome("sma_trend", i, 100, regime="TREND_UP")
            self.outcome("sma_trend", i + 30, 100, known_at=self.now + timedelta(days=1))
        context = self.context()
        record = self.ranking.rank(["orb_vwap", "sma_trend"], context, assessment_id="insufficient")
        self.assertEqual(record["ranked_order"], ["orb_vwap", "sma_trend"])
        self.assertFalse(record["changed"])
        self.assertTrue(all(e["source"] == "AI_RESEARCH_ONLY" for e in record["evidence"]))
        earlier = self.context(self.now - timedelta(days=2))
        self.assertEqual(earlier.outcome_feedback["performance"], [])

    def test_changed_configuration_cannot_borrow_another_version_outcomes(self):
        for i in range(30):
            self.outcome("sma_trend", i, 100)
        from stockmarket.core.strategies.trend import TrendConfig
        self.strategies["sma_trend"] = TrendStrategy(TrendConfig(fast_window=3))
        self.assertEqual(self.context().outcome_feedback["performance"], [])
