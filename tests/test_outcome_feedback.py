import unittest
from datetime import datetime, timedelta, timezone

from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, OrderSide
from stockmarket.core.outcome_feedback import OutcomeFeedbackService
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.strategies import OrbVwapStrategy
from stockmarket.core.strategies.base import strategy_parameters_hash


class OutcomeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.db = SQLiteDatabase()
        migrate(self.db)
        self.addCleanup(self.db.close)
        self.store = Store(self.db)
        self.at = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
        self.instrument = default_markets().get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=.01)
        self.store.instruments.save(self.instrument)
        self.portfolio = PortfolioManager("USD", 100_000)
        self.feedback = OutcomeFeedbackService(self.store, clock=lambda: self.at)
        self.strategy = OrbVwapStrategy()
        self.counter = 0

    def fill(self, side, quantity, price, fee=1, slippage=.2, *, same_time=False):
        self.counter += 1
        if not same_time:
            self.at += timedelta(minutes=1)
        oid = f"order-{self.counter}"
        self.store.execution_records.save(
            client_order_id=oid, mode="PAPER", strategy_name="orb_vwap",
            strategy_version="1.0.0", parameter_version=None,
            parameters_hash=strategy_parameters_hash(self.strategy), timeframe="5m",
            signal_id=None, market_regime="TRENDING", data_timestamp=self.at,
            decision_timestamp=self.at, versioned=True)
        self.store.order_audit.record(
            oid, technical_signals={
                "timeframe": "5m", "strategy_version": "1.0.0",
                "parameters_hash": strategy_parameters_hash(self.strategy),
                "reasons": ["FIXTURE_ENTRY"]}, exit_reason="FIXTURE_EXIT")
        record = self.portfolio.apply_fill(
            self.instrument, side, quantity, price, self.at,
            fee=fee, slippage=slippage, client_order_id=oid, fill_sequence=1)
        self.store.fills.save(record)
        self.feedback.process()
        return record

    def test_partial_additions_reopen_short_fees_and_idempotency(self):
        self.fill(OrderSide.BUY, 10, 100)
        self.fill(OrderSide.BUY, 10, 110)
        self.fill(OrderSide.SELL, 5, 120)
        self.assertEqual(self.store.paper_outcomes.recent(), [])
        self.fill(OrderSide.SELL, 15, 115)
        outcome = self.store.paper_outcomes.recent()[0]
        self.assertEqual(outcome["gross_pnl"], 225)
        self.assertEqual(outcome["fees"], 4)
        self.assertEqual(outcome["net_pnl"], 221)
        self.assertEqual(outcome["entry_price"], 105)
        self.assertAlmostEqual(outcome["slippage"], .8)
        self.fill(OrderSide.SELL, 10, 120)
        record = self.fill(OrderSide.BUY, 10, 110)
        self.store.fills.save(record)
        self.assertEqual(self.feedback.process(), 0)
        self.assertEqual(len(self.store.paper_outcomes.recent()), 2)
        self.assertEqual(self.store.paper_outcomes.recent()[0]["net_pnl"], 98)
        self.assertEqual(self.portfolio.cash["USD"], 100_319)
        events = [e for e in self.store.audit.entries() if e["action"] == "PAPER_OUTCOME_RECORDED"]
        self.assertEqual(len(events), 2)
        self.assertEqual(self.store.audit.verify_chain(), [])
        metrics = self.feedback.performance(self.at)[0]
        self.assertEqual(metrics["net_pnl"], 319)
        self.assertEqual(metrics["sample_status"], "INSUFFICIENT_EVIDENCE")

    def test_reversal_allocates_fee_without_double_counting(self):
        self.fill(OrderSide.BUY, 10, 100, fee=2)
        self.fill(OrderSide.SELL, 15, 110, fee=3)
        self.fill(OrderSide.BUY, 5, 105, fee=1)
        outcomes = self.store.paper_outcomes.recent()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(o["fees"] for o in outcomes), 6)
        self.assertEqual(sum(o["net_pnl"] for o in outcomes), 119)
        self.assertEqual(self.portfolio.cash["USD"], 100_119)

    def test_known_at_prevents_backfill_lookahead_and_changes_conflict(self):
        self.fill(OrderSide.BUY, 10, 100)
        close = self.fill(OrderSide.SELL, 10, 110)
        self.assertEqual(self.feedback.performance(self.at - timedelta(seconds=1)), [])
        self.at += timedelta(days=1)
        self.assertEqual(self.feedback.process(), 0)
        self.assertEqual(self.store.paper_outcomes.recent()[0]["known_at"], close.timestamp.isoformat())
        self.db.execute("UPDATE fills SET price = 120, realized_pnl = 200 WHERE side = 'SELL'")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.feedback.process()

    def test_same_timestamp_uses_durable_ingestion_order(self):
        self.fill(OrderSide.BUY, 10, 100, same_time=True)
        self.fill(OrderSide.SELL, 10, 110, same_time=True)
        self.assertEqual(self.store.paper_outcomes.recent()[0]["gross_pnl"], 100)
        self.assertEqual(OutcomeFeedbackService(self.store, clock=lambda: self.at).process(), 0)

    def test_sample_threshold_currency_version_and_parameter_matching(self):
        for _ in range(30):
            self.fill(OrderSide.BUY, 1, 100, fee=0)
            self.fill(OrderSide.SELL, 1, 101, fee=0)
        evidence = self.feedback.research_evidence(
            self.at, "US", "USD", {"orb_vwap": self.strategy})
        self.assertEqual(evidence["performance"][0]["trade_count"], 30)
        self.assertEqual(evidence["performance"][0]["sample_status"], "QUALIFIED_PAPER_SAMPLE")
        self.assertFalse(evidence["performance"][0]["out_of_sample_validated"])
        self.assertEqual(self.feedback.research_evidence(
            self.at, "US", "INR", {"orb_vwap": self.strategy})["performance"], [])

    def test_backfilled_outcome_not_known_before_processing(self):
        self.fill(OrderSide.BUY, 1, 100)
        self.fill(OrderSide.SELL, 1, 101)
        # A separate empty projection simulates installing feedback after trading.
        self.db.execute("DELETE FROM paper_outcomes")
        close_at = self.at
        self.at += timedelta(days=1)
        self.assertEqual(self.feedback.process(), 1)
        self.assertEqual(self.feedback.performance(close_at), [])
        self.assertEqual(len(self.feedback.performance(self.at)), 1)

    def test_mixed_attribution_cannot_enter_strategy_feedback(self):
        self.fill(OrderSide.BUY, 1, 100)
        self.fill(OrderSide.BUY, 1, 100)
        self.db.execute(
            "UPDATE execution_records SET strategy_version = ? WHERE client_order_id = ?",
            ("different-version", "order-2"))
        self.fill(OrderSide.SELL, 2, 110)
        self.assertEqual(self.store.paper_outcomes.recent()[0]["attribution_status"], "MIXED")
        self.assertEqual(self.feedback.research_evidence(
            self.at, "US", "USD", {"orb_vwap": self.strategy})["performance"], [])

    def test_ambiguous_legacy_ordering_and_bad_realized_pnl_fail_explicitly(self):
        self.fill(OrderSide.BUY, 1, 100, same_time=True)
        self.fill(OrderSide.SELL, 1, 101, same_time=True)
        self.db.execute("DELETE FROM paper_feedback_fills")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.feedback.process()

    def test_outcome_projection_failure_rolls_back_close_fill_and_cash(self):
        from unittest.mock import patch
        self.fill(OrderSide.BUY, 1, 100)
        before_cash = self.portfolio.cash.copy()
        before_fills = self.store.fills.all()
        def persist(fill):
            with self.db.transaction():
                self.store.fills.save(fill)
                self.feedback.process()
        self.portfolio.on_fill = persist
        self.at += timedelta(minutes=1)
        with patch.object(self.store.paper_outcomes, "save", side_effect=RuntimeError("fixture disk error")):
            with self.assertRaisesRegex(RuntimeError, "fixture disk error"):
                self.portfolio.apply_fill(
                    self.instrument, OrderSide.SELL, 1, 101, self.at,
                    client_order_id="close-failed", fill_sequence=1)
        self.assertEqual(self.portfolio.cash, before_cash)
        self.assertEqual(self.store.fills.all(), before_fills)
        self.assertEqual(self.store.paper_outcomes.recent(), [])
