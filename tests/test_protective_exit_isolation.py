from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sqlite3
from unittest import TestCase
from unittest.mock import patch

import test_paper_end_to_end as fixtures
import test_trade_intent as intent
from stockmarket.core.models import OrderSide, OrderStatus, OrderType
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.persistence.repositories import _pages
from stockmarket.core.recovery import RecoveryError, rebuild_portfolio
from stockmarket.core.trading_service import OrderTicket


class ProtectiveExitIsolationTests(TestCase):
    def setUp(self):
        self.f = fixtures.PaperEndToEndTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = self.f.context(auto_loop=True)
        self.client = self.f.client(self.ctx)
        proposal = self.f.propose(self.client)
        opened = self.ctx.paper_proposal_execution.submit(
            proposal["proposal_id"], operator="fixture", sizing_mode="MANUAL_OVERRIDE", quantity=10)
        self.assertEqual(opened["order"].status, OrderStatus.FILLED)
        self.manager = self.ctx.paper_position_manager
        self.monitor = self.ctx.paper_loop.protective_monitor
        self.monitor.enable()
        self.monitor.recovery_complete = True
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.f.data.price = 100.
        self.f.clock.at += timedelta(seconds=1)

    def entry(self, cid="entry-during-monitor"):
        return OrderTicket(self.f.instrument_id, OrderSide.BUY, 1, OrderType.MARKET,
                           "orb_vwap", client_order_id=cid, stop_loss=99., take_profit=106.)

    def test_slow_quote_releases_execution_lock_blocks_entries_and_duplicate_monitor(self):
        entered, release = Event(), Event()
        original = self.manager.quotes

        def slow(iid):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("quote not released")
            return original(iid)

        with patch.object(self.manager, "quotes", side_effect=slow):
            with patch.object(self.ctx.paper, "_quotes", side_effect=AssertionError("nested provider read")):
                with ThreadPoolExecutor(max_workers=1) as workers:
                    future = workers.submit(self.monitor.run_once)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertTrue(self.ctx.paper._submit_lock.acquire(timeout=.1))
                        self.ctx.paper._submit_lock.release()
                        result = self.ctx.paper.submit(self.entry())
                        self.assertEqual(result.risk.reason, "PROTECTIVE_RECONCILIATION_IN_PROGRESS")
                        self.assertEqual(self.manager.manage()["error"], "MONITORING_BUSY")
                        self.assertFalse(future.done())
                    finally:
                        release.set()
                    first = future.result(timeout=5)
                    self.assertEqual(first["result"]["error"], "MONITORING_STATE_CHANGED")
            self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(self.ctx.paper.portfolio.positions(), {})
        self.assertEqual(len(self.f.store.fills.all()), 2)
        self.assertEqual(len(self.f.store.db.query("SELECT * FROM position_exit_proposals")), 1)
        for _ in range(3):
            self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)
        exit_id = self.f.store.db.query("SELECT client_order_id FROM position_exit_proposals")[0]["client_order_id"]
        audit = self.f.store.order_audit.get(exit_id)
        self.assertTrue(audit["risk_inputs"]["exit_quote_preparation"]["complete"])
        q = audit["risk_inputs"]["exit_quote_preparation"]["quotes"][self.f.instrument_id]
        self.assertEqual(q["instrument_id"], self.f.instrument_id)
        self.assertEqual(q["price"], 100.)

    def test_quote_expires_after_preparation_and_before_execution(self):
        original = self.manager._prepare_monitoring

        def expires(*args, **kwargs):
            prepared = original(*args, **kwargs)
            self.f.clock.at += self.manager.max_age + timedelta(seconds=1)
            return prepared

        with patch.object(self.manager, "_prepare_monitoring", side_effect=expires):
            result = self.monitor.run_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.assertEqual(len(self.f.store.fills.all()), 1)
        self.assertEqual(self.ctx.paper.submit(self.entry()).risk.reason,
                         "PROTECTIVE_RECONCILIATION_INCOMPLETE")
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)

    def test_portfolio_revision_change_rejects_whole_prepared_pass(self):
        original = self.manager.quotes

        def changed(iid):
            result = original(iid)
            with self.ctx.paper._submit_lock:
                checkpoint = self.ctx.paper.portfolio.execution_checkpoint()
                self.ctx.paper.portfolio.deposit("USD", 1.)
                self.ctx.paper.portfolio.restore_execution_checkpoint(checkpoint)
            return result

        with patch.object(self.manager, "quotes", side_effect=changed):
            result = self.monitor.run_once()
        self.assertEqual(result["result"]["error"], "MONITORING_STATE_CHANGED")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(self.f.store.fills.all()), 1)
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)

    def test_concurrent_pending_entry_cancellation_invalidates_prepared_exit(self):
        self.f.data.price = 102.
        pending = self.ctx.paper.submit(OrderTicket(
            self.f.instrument_id, OrderSide.BUY, 1, OrderType.LIMIT, "orb_vwap",
            client_order_id="cancel-concurrently", limit_price=101., stop_loss=99., take_profit=106.))
        self.assertEqual(pending.order.status, OrderStatus.ACCEPTED, pending.risk.reason)
        entered, release = Event(), Event()
        original = self.manager.quotes

        def slow(iid):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("quote not released")
            return original(iid)

        with patch.object(self.manager, "quotes", side_effect=slow):
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(self.monitor.run_once)
                try:
                    self.assertTrue(entered.wait(5))
                    cancelled = self.ctx.paper.cancel("cancel-concurrently")
                    self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
                finally:
                    release.set()
                result = future.result(timeout=5)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["result"]["error"], "MONITORING_STATE_CHANGED")
        self.f.data.price = 100.
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)

    def test_missing_quote_blocks_entries_until_successful_retry(self):
        with patch.object(self.manager, "quotes", return_value=None):
            result = self.monitor.run_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(self.f.store.fills.all()), 1)
        self.assertEqual(self.ctx.paper.submit(self.entry()).risk.status.value, "REJECTED")
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.fills.all()), 2)

    def test_external_order_change_prevents_recovery_and_new_entry(self):
        self.f.store.db.execute("UPDATE orders SET quantity = quantity + 1")
        with self.assertRaisesRegex(RecoveryError, "order replay incomplete"):
            self.ctx.recovery.recover()
        self.assertTrue(self.ctx.gate.halted)
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_STATE_STALE"):
            self.ctx.paper.submit(self.entry())

    def test_external_fill_price_change_is_detected_before_monitoring_mutation(self):
        self.f.store.db.execute("UPDATE fills SET price = price + 1")
        result = self.monitor.run_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("EXECUTION_STATE_STALE", result["error"])
        self.assertTrue(self.ctx.gate.halted)
        self.assertEqual(len(self.f.store.fills.all()), 1)

    def test_resume_cannot_bypass_interrupted_history_replay(self):
        original = self.f.store.orders.iter_all

        def interrupted():
            yield from original(page_size=1)
            raise RuntimeError("replay completion marker unavailable")

        with patch.object(self.f.store.orders, "iter_all", side_effect=interrupted):
            with self.assertRaisesRegex(RecoveryError, "replay incomplete"):
                self.ctx.recovery.recover()
        with self.assertRaisesRegex(RecoveryError, "replay incomplete"):
            self.ctx.recovery.resume("fixture", "cannot acknowledge an incomplete replay", ["ALL"])
        self.assertTrue(self.ctx.gate.halted)
        self.assertTrue(self.ctx.recovery.recover().clean)
        self.assertFalse(self.ctx.gate.halted)

    def test_mid_transition_failure_never_reports_durable_submission(self):
        service = self.ctx.paper
        original = service.order_manager.on_change

        def fail_submitted(order, event):
            if event.to_status is OrderStatus.SUBMITTED:
                raise RuntimeError("interrupted before durable submission")
            original(order, event)

        self.f.data.price = 102.
        with patch.object(service.order_manager, "on_change", side_effect=fail_submitted):
            with self.assertRaisesRegex(RuntimeError, "durable submission"):
                result = service.submit(self.entry("interrupted-submission"))
                self.fail(f"submission did not reach persistence: {result.risk.reason}")
        local = service.order_manager.get("interrupted-submission")
        durable = self.f.store.orders.get(local.client_order_id)
        self.assertEqual(local.status, OrderStatus.VALIDATED)
        self.assertEqual(durable["status"], "VALIDATED")
        self.assertIsNone(self.f.store.paper_execution.get_by_client_order_id(local.client_order_id))
        self.assertTrue(self.ctx.gate.halted)
        self.assertEqual(len(self.f.store.fills.all()), 1)

    def test_database_contention_halts_recovery_and_fails_monitoring_until_retry(self):
        db = self.f.store.db
        db.lock_timeout = .01
        entered, release = Event(), Event()

        def held():
            with db.transaction():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("transaction not released")

        with ThreadPoolExecutor(max_workers=1) as workers:
            owner = workers.submit(held)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(Exception, "DATABASE_LOCK_TIMEOUT"):
                    self.ctx.recovery.recover()
                self.assertTrue(self.ctx.gate.halted)
                self.assertEqual(self.monitor.run_once()["status"], "FAILED")
                self.assertFalse(self.monitor.status()["entry_ready"])
            finally:
                release.set()
            owner.result(timeout=5)
        self.assertTrue(self.ctx.recovery.recover().clean)
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")


class RecoverySnapshotTests(TestCase):
    def setUp(self):
        self.f = intent.TradingServiceIntentTests()
        self.f.setUp()
        self.addCleanup(self.f.db.close)
        self.f.service.submit(self.f.ticket(OrderSide.BUY, cid="one"))
        self.f.service.submit(self.f.ticket(OrderSide.BUY, cid="two"))

    def test_missing_page_never_completes_even_if_keyset_query_returns_empty(self):
        original = self.f.db.query

        def missing(sql, params=()):
            if "WHERE p.ordinal > ?" in sql and params[0] > 0:
                return []
            return original(sql, params)

        with patch.object(self.f.db, "query", side_effect=missing):
            with self.assertRaisesRegex(ValueError, "HISTORY_SNAPSHOT_CHANGED"):
                list(self.f.store.fills.iter_all(page_size=1))

    def test_duplicate_or_reversed_page_is_rejected(self):
        row = {"timestamp": "2026-10-01", "id": "one"}
        with self.assertRaisesRegex(ValueError, "HISTORY_ORDER_INVALID"):
            list(_pages(lambda cursor, limit: [row, row], "id", page_size=2, max_rows=10))

    def test_missing_durable_ordinal_is_rejected(self):
        self.f.db.execute("DELETE FROM paper_feedback_fills WHERE ordinal = 1")
        with self.assertRaisesRegex(ValueError, "HISTORY_ORDINAL_GAP"):
            list(self.f.store.fills.iter_all(page_size=1))

    def test_interrupted_replay_can_restart_without_partial_publication(self):
        iterator = self.f.store.fills.iter_all(page_size=1)
        next(iterator)
        iterator.close()
        for _ in range(2):
            rebuilt = rebuild_portfolio(self.f.store, self.f.service.instruments, "USD", 50000)
            self.assertEqual(rebuilt.cash, self.f.portfolio.cash)
            self.assertEqual(len(rebuilt.fills), 2)

    def test_external_same_count_update_during_replay_invalidates_snapshot(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "isolated-history.db")
            one, two = SQLiteDatabase(path), SQLiteDatabase(path)
            try:
                migrate(one)
                store = Store(one)
                store.instruments.save(next(iter(self.f.service.instruments.values())))
                for fill in self.f.portfolio.fills:
                    store.fills.save(fill)
                iterator = store.fills.iter_all(page_size=1)
                next(iterator)
                two.execute("UPDATE fills SET price = price + 1")
                with self.assertRaisesRegex(ValueError, "HISTORY_SNAPSHOT_CHANGED"):
                    list(iterator)
                self.assertEqual(len(list(store.fills.iter_all(page_size=1))), 2)
            finally:
                two.close()
                one.close()

    def test_unrelated_shared_connection_write_does_not_invalidate_history_replay(self):
        # Heartbeats, audit and research writes share the connection; they are not fill/order history.
        fills = self.f.store.fills.iter_all(page_size=1)
        orders = self.f.store.orders.iter_all(page_size=1)
        next(fills)
        next(orders)
        self.f.db.execute("CREATE TABLE IF NOT EXISTS unrelated_fixture (value INTEGER)")
        self.f.db.execute("INSERT INTO unrelated_fixture VALUES (1)")
        self.assertEqual(len(list(fills)), 1)
        self.assertEqual(len(list(orders)), 1)

    def test_same_connection_same_count_history_update_invalidates_replay(self):
        for table, iterate in (("fills", self.f.store.fills.iter_all),
                               ("orders", self.f.store.orders.iter_all)):
            with self.subTest(table=table):
                iterator = iterate(page_size=1)
                next(iterator)
                column = "price" if table == "fills" else "quantity"
                self.f.db.execute(f"UPDATE {table} SET {column} = {column}")  # nosec B608 - fixed test identifiers
                with self.assertRaisesRegex(ValueError, "HISTORY_SNAPSHOT_CHANGED"):
                    list(iterator)

    def test_same_connection_feedback_ordinal_change_invalidates_replay(self):
        iterator = self.f.store.fills.iter_all(page_size=1)
        next(iterator)
        self.f.db.execute("UPDATE paper_feedback_fills SET ordinal = ordinal")
        with self.assertRaisesRegex(ValueError, "HISTORY_SNAPSHOT_CHANGED"):
            list(iterator)

    def test_concurrent_wal_reader_and_writer_observe_only_committed_state(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "isolated-concurrency.db")
            one, two = SQLiteDatabase(path), SQLiteDatabase(path, busy_timeout=.01)
            try:
                one.execute("CREATE TABLE fixture (value INTEGER)")
                one.execute("INSERT INTO fixture VALUES (1)")
                entered, release = Event(), Event()

                def write():
                    with one.transaction():
                        one.execute("UPDATE fixture SET value = 2")
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("writer not released")

                with ThreadPoolExecutor(max_workers=1) as workers:
                    future = workers.submit(write)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(two.query("SELECT value FROM fixture"), [{"value": 1}])
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            with two.transaction():
                                two.execute("UPDATE fixture SET value = 3")
                    finally:
                        release.set()
                    future.result(timeout=5)
                self.assertEqual(two.query("SELECT value FROM fixture"), [{"value": 2}])
                with two.transaction():
                    two.execute("UPDATE fixture SET value = 3")
                self.assertEqual(one.query("SELECT value FROM fixture"), [{"value": 3}])
            finally:
                two.close()
                one.close()

    def test_real_history_above_processing_ceiling_never_returns_partial_portfolio(self):
        db = self.f.db
        db.statement_timeout = 10.
        db.execute("DELETE FROM paper_feedback_fills")
        db.execute("DELETE FROM fills")
        iid = self.f.ticket(OrderSide.BUY).instrument_id
        db.execute(
            """WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x < 100001)
               INSERT INTO fills (fill_id, instrument_id, side, quantity, price, fee, slippage,
                                  currency, realized_pnl, timestamp)
               SELECT printf('large-%06d', x), ?, CASE WHEN x%2=1 THEN 'BUY' ELSE 'SELL' END,
                      1, 100, 0, 0, 'USD', 0, ? FROM n""", (iid, intent.NOW.isoformat()))
        db.execute(
            """INSERT INTO paper_feedback_fills (fill_id, ordinal)
               SELECT fill_id, CAST(substr(fill_id, 7) AS INTEGER) FROM fills""")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM fills")[0]["n"], 100001)
        with self.assertRaisesRegex(ValueError, "HISTORY_LIMIT_EXCEEDED"):
            self.f.store.fills.execution_state()
        with self.assertRaisesRegex(ValueError, "HISTORY_LIMIT_EXCEEDED"):
            rebuild_portfolio(self.f.store, self.f.service.instruments, "USD", 50000)
        before = len(self.f.orders.orders())
        with self.assertRaisesRegex(ValueError, "HISTORY_LIMIT_EXCEEDED"):
            self.f.service.submit(self.f.ticket(OrderSide.BUY, cid="over-ceiling"))
        self.assertEqual(len(self.f.orders.orders()), before)
        self.assertEqual(len(self.f.portfolio.fills), 2)
