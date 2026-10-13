from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import test_trade_intent as intent_fixtures
import test_paper_end_to_end as paper_fixtures
from stockmarket.core.data.resilient import DataPolicy, ResilientProvider
from stockmarket.core.data.provider import DataUnavailable
from stockmarket.core.models import OrderSide
from stockmarket.core.risk import OrderIntent
from stockmarket.core.persistence import SQLiteDatabase
from stockmarket.core.persistence.database import DatabaseError
from stockmarket.core.recovery import RecoveryError, rebuild_portfolio
from stockmarket.core.resilience import CircuitBreaker, RetryPolicy
from stockmarket.core.trading_gate import TradingGate
from stockmarket.core.trading_service import OrderTicket
from stockmarket.core.models import OrderType


class PreparedEntryTests(TestCase):
    def setUp(self):
        self.f = intent_fixtures.TradingServiceIntentTests()
        self.f.setUp()
        self.addCleanup(self.f.db.close)
        self.service = self.f.service
        self.ticket = self.f.ticket(OrderSide.BUY, cid="prepared")

    def test_immutable_timestamped_preparation_and_final_risk(self):
        prepared = self.service.prepare_entry(self.ticket.instrument_id)
        self.assertEqual(prepared.started_at, intent_fixtures.NOW)
        self.assertTrue(prepared.complete)
        with self.assertRaises(TypeError):
            prepared.statistics["correlated_exposure"] = 0
        with patch.object(self.service._risk, "evaluate_proposal",
                          wraps=self.service._risk.evaluate_proposal) as risk:
            result = self.service.submit(self.ticket, prepared=prepared)
        self.assertEqual(risk.call_count, 1)
        self.assertEqual(result.risk.status.value, "APPROVED")

    def test_stale_statistics_rejected_even_with_a_fresh_quote(self):
        prepared = self.service.prepare_entry(self.ticket.instrument_id)
        self.service._clock = lambda: intent_fixtures.NOW + timedelta(minutes=10)
        result = self.service.submit(self.ticket, prepared=prepared)
        self.assertEqual(result.risk.reason, "PREPARED_INPUTS_STALE")
        self.assertEqual(self.f.portfolio.fills, ())

    def test_changed_cash_rejected_before_submission(self):
        prepared = self.service.prepare_entry(self.ticket.instrument_id)
        self.f.portfolio.deposit("USD", 1)
        result = self.service.submit(self.ticket, prepared=prepared)
        self.assertEqual(result.risk.reason, "PREPARED_STATE_CHANGED")
        self.assertEqual(self.f.portfolio.fills, ())

    def test_final_kill_gate_overrides_preparation(self):
        prepared = self.service.prepare_entry(self.ticket.instrument_id)
        self.service._gate = TradingGate()
        self.service._gate.halt("TEST", "risk changed")
        result = self.service.submit(self.ticket, prepared=prepared)
        self.assertIn("TRADING_HALTED", result.risk.reason)
        self.assertEqual(self.f.portfolio.fills, ())

    def test_restored_identical_state_cannot_hide_a_transient_risk_change(self):
        prepared = self.service.prepare_entry(self.ticket.instrument_id)
        checkpoint = self.f.portfolio.execution_checkpoint()
        self.f.portfolio.deposit("USD", 1)
        self.f.portfolio.restore_execution_checkpoint(checkpoint)
        result = self.service.submit(self.ticket, prepared=prepared)
        self.assertEqual(result.risk.reason, "PREPARED_STATE_CHANGED")
        self.assertEqual(self.f.portfolio.fills, ())

    def test_unavailable_quote_never_becomes_a_price(self):
        with patch.object(self.service, "_quotes", return_value=None):
            prepared = self.service.prepare_entry(self.ticket.instrument_id)
        self.assertFalse(prepared.complete)
        result = self.service.submit(self.ticket, prepared=prepared)
        self.assertEqual(result.risk.status.value, "REJECTED")
        self.assertEqual(self.f.portfolio.fills, ())

    def test_nonfinite_or_mutable_statistics_fail_before_any_order(self):
        for values in ({"correlated_exposure": float("nan")}, {"correlated_exposure": []}):
            with self.subTest(values=values):
                with patch.object(self.service, "_stats", return_value=values):
                    with self.assertRaises(ValueError):
                        self.service.prepare_entry(self.ticket.instrument_id)
        self.assertEqual(self.f.orders.orders(), ())

    def test_concurrent_same_order_remains_idempotent(self):
        synchronized = Barrier(2)

        def statistics(iid):
            synchronized.wait(timeout=5)
            return {}

        with patch.object(self.service, "_stats", side_effect=statistics):
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(self.service.submit, self.ticket) for _ in range(2)]
                results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(result.duplicate for result in results), 1)
        self.assertEqual(len(self.f.portfolio.fills), 1)
        self.assertEqual(len(self.f.orders.orders()), 1)

    def test_history_pages_and_restart_replay_are_complete(self):
        self.service.submit(self.ticket)
        self.service.submit(self.f.ticket(OrderSide.BUY, cid="second"))
        pages = list(self.f.store.fills.iter_all(page_size=1))
        self.assertEqual(len(pages), 2)
        self.assertEqual(len({row["fill_id"] for row in pages}), 2)
        self.assertEqual(len(list(self.f.store.orders.iter_all(page_size=1))), 2)
        original = self.f.store.fills.iter_all
        with patch.object(self.f.store.fills, "iter_all", side_effect=lambda: original(page_size=1)):
            restored = rebuild_portfolio(self.f.store, self.service.instruments, "USD", 50000)
        self.assertEqual(restored.cash, self.f.portfolio.cash)
        self.assertEqual(len(restored.fills), 2)
        with self.assertRaisesRegex(ValueError, "HISTORY_LIMIT_EXCEEDED"):
            list(original(page_size=1, max_rows=1))
        with self.assertRaisesRegex(ValueError, "HISTORY_LIMIT_EXCEEDED"):
            self.f.store.orders.execution_state(limit=1)

    def test_interrupted_fill_replay_returns_no_partial_portfolio(self):
        def interrupted():
            yield {"instrument_id": self.ticket.instrument_id, "side": "BUY", "quantity": 1,
                   "price": 100., "fee": 0., "slippage": 0.,
                   "timestamp": intent_fixtures.NOW.isoformat()}
            raise RecoveryError("page failed")
        with patch.object(self.f.store.fills, "iter_all", side_effect=interrupted):
            with self.assertRaisesRegex(RecoveryError, "page failed"):
                rebuild_portfolio(self.f.store, self.service.instruments, "USD", 50000)

    def test_same_timestamp_round_trip_replays_by_execution_ordinal_across_pages(self):
        self.service.submit(self.ticket)
        self.service.submit(self.f.ticket(OrderSide.SELL, purpose=OrderIntent.EXIT, cid="closed"))
        original = self.f.store.fills.iter_all
        with patch.object(self.f.store.fills, "iter_all", side_effect=lambda: original(page_size=1)):
            restored = rebuild_portfolio(self.f.store, self.service.instruments, "USD", 50000)
        self.assertEqual(restored.positions(), {})
        self.assertEqual(restored.cash, self.f.portfolio.cash)
        self.assertEqual([row["client_order_id"] for row in original(page_size=1)],
                         ["prepared", "closed"])

    def test_query_plans_use_migration_indexes(self):
        for sql, params, index in (
            ("SELECT * FROM fills WHERE (timestamp, fill_id) > (?, ?) "
             "ORDER BY timestamp, fill_id LIMIT ?", ("", "", 10), "idx_fills_replay"),
            ("SELECT * FROM fills WHERE instrument_id = ? AND timestamp >= ? "
             "ORDER BY timestamp, fill_id LIMIT ?", (self.ticket.instrument_id, "", 10),
             "idx_fills_position"),
            ("SELECT * FROM orders WHERE (timestamp, client_order_id) > (?, ?) "
             "ORDER BY timestamp, client_order_id LIMIT ?", ("", "", 10), "idx_orders_replay"),
        ):
            plan = self.f.db.query("EXPLAIN QUERY PLAN " + sql, params)
            self.assertIn(index, str(plan))
            self.assertNotIn("TEMP B-TREE", str(plan))
        ordinal_plan = self.f.db.query(
            "EXPLAIN QUERY PLAN SELECT f.* FROM paper_feedback_fills p "
            "JOIN fills f ON f.fill_id = p.fill_id WHERE p.ordinal > ? ORDER BY p.ordinal LIMIT ?",
            (0, 10))
        self.assertIn("ordinal>?", str(ordinal_plan))
        self.assertNotIn("TEMP B-TREE", str(ordinal_plan))
        for limit in (-1, True, 100001):
            with self.assertRaises(ValueError):
                self.f.store.fills.execution_state(limit)


class DatabaseIsolationTests(TestCase):
    def test_contention_is_bounded_and_then_recovers(self):
        db = SQLiteDatabase(lock_timeout=.01)
        self.addCleanup(db.close)
        entered, release = Event(), Event()

        def hold():
            with db.transaction():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test transaction not released")

        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(hold)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(DatabaseError, "DATABASE_LOCK_TIMEOUT"):
                    db.query("SELECT 1")
            finally:
                release.set()
            future.result(timeout=5)
        self.assertEqual(db.query("SELECT 1 AS value"), [{"value": 1}])
        self.assertTrue(any(not item["acquired"] for item in db.timings))

    def test_transaction_failure_rolls_back_and_can_be_retried(self):
        db = SQLiteDatabase()
        self.addCleanup(db.close)
        db.execute("CREATE TABLE fixture (value INTEGER)")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            with db.transaction():
                db.execute("INSERT INTO fixture VALUES (1)")
                raise RuntimeError("write failed")
        self.assertEqual(db.query("SELECT * FROM fixture"), [])
        with db.transaction():
            db.execute("INSERT INTO fixture VALUES (2)")
        self.assertEqual(db.query("SELECT * FROM fixture"), [{"value": 2}])

    def test_long_statement_is_interrupted_without_poisoning_connection(self):
        db = SQLiteDatabase()
        self.addCleanup(db.close)
        # A controlled progress deadline, not a machine-speed threshold.
        with patch("stockmarket.core.persistence.database.time.perf_counter",
                   side_effect=[0., 0., 0., 2., 2.]):
            with self.assertRaisesRegex(DatabaseError, "DATABASE_STATEMENT_TIMEOUT"):
                db.query("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                         "SELECT SUM(x) FROM n")
        self.assertEqual(db.query("SELECT 1 AS value"), [{"value": 1}])


class ProviderIsolationTests(TestCase):
    def test_timeout_late_results_capacity_and_close_are_bounded(self):
        release = Event()
        calls = []

        def vendor():
            calls.append(1)
            if not release.wait(5):
                raise RuntimeError("vendor not released")
            return "late value"

        provider = ResilientProvider(
            SimpleNamespace(name="fixture", research_only=True),
            policy=DataPolicy(timeout_seconds=.01, retry=RetryPolicy(max_attempts=1)),
            breaker=CircuitBreaker(failure_threshold=100))
        self.addCleanup(provider.close)
        futures = []
        submit = provider._pool.submit

        def capture(fn):
            future = submit(fn)
            futures.append(future)
            return future

        try:
            with patch.object(provider._pool, "submit", side_effect=capture):
                for _ in range(4):
                    with self.assertRaisesRegex(DataUnavailable, "ProviderTimeout"):
                        provider._call(vendor)
                for _ in range(25):
                    with self.assertRaisesRegex(DataUnavailable, "capacity exhausted"):
                        provider._call(vendor)
            self.assertEqual(len(futures), 4)
            self.assertEqual(len(calls), 4)
        finally:
            release.set()
        completed = [Event() for _ in futures]
        for future, done in zip(futures, completed):
            future.add_done_callback(lambda _, done=done: done.set())
        self.assertTrue(all(done.wait(5) for done in completed))
        self.assertEqual(provider._call(lambda: "new value"), "new value")
        provider.close()
        with self.assertRaisesRegex(DataUnavailable, "closed"):
            provider._call(vendor)

    def test_shutdown_during_a_running_read_never_delivers_late_result(self):
        release = Event()
        returned = Event()
        provider = ResilientProvider(
            SimpleNamespace(name="fixture", research_only=True),
            policy=DataPolicy(timeout_seconds=.01, retry=RetryPolicy(max_attempts=1)))
        self.addCleanup(provider.close)

        def read():
            if not release.wait(5):
                raise RuntimeError("read not released")
            returned.set()
            return 100.

        try:
            with self.assertRaisesRegex(DataUnavailable, "ProviderTimeout"):
                provider._call(read)
            provider.close()
            self.assertFalse(returned.is_set())
            with self.assertRaisesRegex(DataUnavailable, "closed"):
                provider._call(lambda: 101.)
        finally:
            release.set()
        self.assertTrue(returned.wait(5))


class ReplayIsolationTests(TestCase):
    def setUp(self):
        self.f = paper_fixtures.PaperEndToEndTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = self.f.context(auto_loop=True)
        self.client = self.f.client(self.ctx)

    def test_protective_exit_completes_while_entry_history_is_blocked(self):
        proposal = self.f.propose(self.client)
        self.assertEqual(self.f.submit(self.client, proposal["proposal_id"]).status_code, 200)
        monitor = self.ctx.paper_loop.protective_monitor
        monitor.enable()
        monitor.recovery_complete = True
        self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        entered, release = Event(), Event()

        def history(iid):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("history not released")
            return paper_fixtures.market_stats(iid)

        ticket = OrderTicket(self.f.instrument_id, OrderSide.BUY, 1, OrderType.MARKET,
                             "orb_vwap", client_order_id="blocked-entry",
                             stop_loss=101., take_profit=106.)
        with patch.object(self.ctx.paper, "_stats", side_effect=history):
            with ThreadPoolExecutor(max_workers=1) as worker:
                future = worker.submit(self.ctx.paper.submit, ticket)
                try:
                    self.assertTrue(entered.wait(5))
                    self.f.data.price = 100.
                    self.f.clock.at += timedelta(seconds=1)
                    self.assertEqual(monitor.run_once()["status"], "HEALTHY")
                    self.assertEqual(self.ctx.paper.portfolio.positions(), {})
                    self.assertFalse(future.done())
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5).risk.reason, "PREPARED_STATE_CHANGED")
        for _ in range(3):
            self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)
        self.assertEqual(len(self.f.store.db.query("SELECT proposal_id FROM position_exit_proposals")), 1)

    def test_interrupted_order_recovery_halts_and_never_reports_complete(self):
        proposal = self.f.propose(self.client)
        self.assertEqual(self.f.submit(self.client, proposal["proposal_id"]).status_code, 200)
        original = self.f.store.orders.iter_all

        def interrupted():
            for row in original(page_size=1):
                yield row
                raise RuntimeError("second page unavailable")

        with patch.object(self.f.store.orders, "iter_all", side_effect=interrupted):
            with self.assertRaisesRegex(RecoveryError, "order replay incomplete"):
                self.ctx.recovery.recover()
        self.assertTrue(self.ctx.gate.halted)

    def test_interrupted_evidence_replay_rolls_back_current_decision_and_retries(self):
        proposal = self.f.propose(self.client)
        self.assertEqual(self.f.submit(self.client, proposal["proposal_id"]).status_code, 200)
        decision = self.f.store.prospective.decisions()[0]
        original = self.ctx.prospective._attach
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("evidence read interrupted")
            return original(*args, **kwargs)

        with patch.object(self.ctx.prospective, "_attach", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "evidence read interrupted"):
                self.ctx.prospective.reconcile()
        self.assertEqual(self.f.store.prospective.attachments(decision["decision_id"]), [])
        self.assertGreater(self.ctx.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(self.ctx.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(self.ctx.prospective.readiness()["training"], "NOT_PERFORMED")

    def test_scheduling_delay_is_observed_against_a_controlled_clock(self):
        monitor = self.ctx.paper_loop.protective_monitor
        monitor.enable()
        monitor.monotonic = lambda: 5.
        monitor._scheduled_at = 2.
        self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(monitor.status()["scheduling_delay_seconds"], 3.)

    def test_expired_evidence_budget_never_creates_a_dataset(self):
        self.f.propose(self.client)
        with patch("stockmarket.core.prospective_evidence.perf_counter", side_effect=[0., 2.]):
            with self.assertRaisesRegex(ValueError, "EVIDENCE_REPLAY_BUDGET_EXCEEDED"):
                self.ctx.prospective.reconcile()
        self.assertEqual(self.f.store.db.query("SELECT manifest_id FROM prospective_datasets"), [])
        self.assertTrue(self.ctx.prospective.reconcile()["complete"])
