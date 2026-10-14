from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta
from threading import Event, Lock
from tempfile import TemporaryDirectory
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock, patch

import test_paper_end_to_end as fixtures
from stockmarket.api.bootstrap import build_context
from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import default_markets
from stockmarket.core.protective_monitor import ProtectiveMonitor, ProtectiveMonitorSettings
from stockmarket.core.settings import ConfigurationError, load_settings


class Monotonic:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now


class ProtectiveMonitorTests(TestCase):
    def setUp(self):
        self.f = fixtures.PaperEndToEndTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = self.f.context(auto_loop=True)
        self.client = self.f.client(self.ctx)
        self.monitor = self.ctx.paper_loop.protective_monitor
        self.mono = Monotonic()
        self.monitor.monotonic = self.mono

    def ready(self):
        self.monitor.enable()
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.monitor.recovery_complete = True
        self.assertTrue(self.monitor.status()["entry_ready"])

    def open_position(self):
        proposal = self.f.propose(self.client)
        self.assertEqual(self.f.submit(self.client, proposal["proposal_id"]).status_code, 200)
        return proposal

    def test_slow_research_does_not_hold_monitor_scheduler_or_execution_lock(self):
        self.open_position()
        self.ready()
        entered, release = Event(), Event()

        def slow_research(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test research was not released")
            return {"run_id": "slow-research", "candidates": []}

        original_quote = self.ctx.paper_position_manager.quotes

        def measured_quote(iid):
            self.mono.now += .25
            return original_quote(iid)

        with patch.object(self.ctx.autonomous_research, "run", side_effect=slow_research):
            with ThreadPoolExecutor(max_workers=1) as worker:
                scan = worker.submit(self.ctx.paper_loop.tick)
                try:
                    self.assertTrue(entered.wait(5))
                    self.mono.now += 120.
                    self.f.clock.at += timedelta(minutes=1)
                    self.f.data.price = 100.
                    wall_started = perf_counter()
                    with patch.object(self.ctx.paper_position_manager, "quotes", side_effect=measured_quote):
                        result = self.monitor.run_once()
                    wall_duration = perf_counter() - wall_started
                    self.assertEqual(result["status"], "HEALTHY", result)
                    self.assertFalse(scan.done())
                    self.assertEqual(self.f.state(self.client)["positions"], [])
                    status = self.monitor.status()
                    self.assertEqual(status["last_duration_seconds"], .25)
                    self.assertLess(status["last_duration_seconds"], status["latency_budget_seconds"])
                    self.assertEqual(len(self.f.store.paper_outcomes.recent()), 1)
                    print("PROTECTIVE_MONITOR slow research=120 simulated seconds; "
                          "protective cycle=0.25 simulated seconds; budget=10 seconds; "
                          f"observed wall duration={wall_duration:.6f} seconds; net outcome=-200")
                finally:
                    release.set()
                self.assertEqual(scan.result(timeout=5)["status"], "COMPLETE")
        for _ in range(3):
            self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.orders.all()), 2)
        self.assertEqual(len(self.f.store.db.query("SELECT proposal_id FROM position_exit_proposals")), 1)

    def test_monitor_exception_is_failure_then_success_recovers(self):
        self.ready()
        with patch.object(self.ctx.paper_position_manager, "manage", side_effect=RuntimeError("provider outage")):
            self.assertEqual(self.monitor.run_once()["status"], "FAILED")
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.assertEqual(self.monitor.status()["consecutive_failures"], 1)
        self.assertIn("provider outage", self.monitor.status()["last_error"])
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertTrue(self.monitor.status()["entry_ready"])
        self.assertEqual(self.monitor.status()["consecutive_failures"], 0)

    def test_scheduled_worker_runs_while_scan_is_blocked(self):
        self.open_position()
        self.ready()
        entered, release, monitored = Event(), Event(), Event()

        def research(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test scan was not released")
            return {"run_id": "delayed", "candidates": []}

        original = self.monitor.run_once

        def observe():
            result = original()
            monitored.set()
            return result

        with patch.object(self.ctx.autonomous_research, "run", side_effect=research):
            with ThreadPoolExecutor(max_workers=1) as worker:
                scan = worker.submit(self.ctx.paper_loop.tick)
                try:
                    self.assertTrue(entered.wait(5))
                    self.f.clock.at += timedelta(minutes=1)
                    self.f.data.price = 100.
                    with patch.object(self.monitor, "run_once", side_effect=observe):
                        self.monitor.start()
                        self.assertTrue(monitored.wait(5))
                    self.assertFalse(scan.done())
                    self.assertEqual(self.f.state(self.client)["positions"], [])
                    self.assertEqual(self.monitor.status()["state"], "HEALTHY")
                finally:
                    self.monitor.stop(timeout=5)
                    release.set()
                scan.result(timeout=5)

    def test_disk_reopen_preserves_positions_and_pending_order_reconciliation(self):
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / "paper.db")
            db = fixtures.SQLiteDatabase(path)
            fixtures.migrate(db)
            self.f.store = fixtures.Store(db)
            self.f.store.instruments.save(fixtures.default_markets().get("US").instrument(
                "AAPL", mic="XNAS", asset_class=fixtures.AssetClass.EQUITY, tick_size=.01))
            try:
                ctx = self.f.context(auto_loop=True)
                client = self.f.client(ctx)
                proposal = self.f.propose(client)
                self.assertEqual(self.f.submit(client, proposal["proposal_id"]).status_code, 200)
                self.assertEqual(len(ctx.paper.portfolio.positions()), 1)
                db.close()
                db = fixtures.SQLiteDatabase(path)
                self.f.store = fixtures.Store(db)
                restarted = self.f.context(auto_loop=True)
                monitor = restarted.paper_loop.protective_monitor
                monitor.enable()
                self.assertFalse(monitor.status()["entry_ready"])
                self.assertEqual(monitor.run_once()["status"], "HEALTHY")
                self.assertEqual(len(restarted.paper.portfolio.positions()), 1)
                self.f.clock.at += timedelta(minutes=1)
                self.f.data.price = 100.
                self.assertEqual(monitor.run_once()["status"], "HEALTHY")
                self.assertEqual(len(self.f.store.orders.all()), 2)
                self.assertEqual(len(self.f.store.paper_outcomes.recent()), 1)
            finally:
                db.close()

    def test_slow_historical_provider_request_does_not_prevent_quote_monitoring(self):
        self.open_position()
        self.ready()
        entered, release = Event(), Event()
        original = self.f.data.get_ohlcv
        counter = [0]
        mutex = Lock()

        def slow_history(*args, **kwargs):
            with mutex:
                counter[0] += 1
                if counter[0] == 4:
                    entered.set()
            if not release.wait(5):
                raise RuntimeError("test provider was not released")
            return original(*args, **kwargs)

        instrument = self.ctx.instruments[self.f.instrument_id]
        with patch.object(self.f.data, "get_ohlcv", side_effect=slow_history):
            with ThreadPoolExecutor(max_workers=4) as worker:
                histories = [worker.submit(
                    self.ctx.market_data.get_historical_data, instrument,
                    self.f.clock.at - timedelta(days=1), self.f.clock.at, "5m") for _ in range(4)]
                try:
                    self.assertTrue(entered.wait(5))
                    self.f.clock.at += timedelta(minutes=1)
                    self.f.data.price = 100
                    self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
                    self.assertTrue(all(not history.done() for history in histories))
                    self.assertEqual(self.f.state(self.client)["positions"], [])
                finally:
                    release.set()
                for history in histories:
                    history.result(timeout=5)

    def test_execution_quote_timeout_has_no_retry_and_keeps_position_open(self):
        self.open_position()
        self.ready()
        before = self.f.state(self.client)
        future = Mock()
        future.result.side_effect = FutureTimeout()
        lane = self.ctx.market_data._quote_lane
        with patch.object(lane._pool, "submit", return_value=future) as submit:
            result = self.monitor.run_once()
        self.assertEqual(result["status"], "FAILED")
        submit.assert_called_once()
        future.result.assert_called_once_with(timeout=2.)
        future.cancel.assert_called_once()
        self.assertEqual(self.f.state(self.client), before)
        self.assertFalse(self.monitor.status()["entry_ready"])

    def test_stalled_cycle_watchdog_single_flight_and_shutdown(self):
        proposal = self.f.propose(self.client)
        self.ready()
        entered, release = Event(), Event()

        def stalled(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test monitoring was not released")
            return {"positions": [], "orders": []}

        with patch.object(self.ctx.paper_position_manager, "manage", side_effect=stalled):
            with ThreadPoolExecutor(max_workers=1) as worker:
                cycle = worker.submit(self.monitor.run_once)
                try:
                    self.assertTrue(entered.wait(5))
                    self.mono.now += 11
                    self.assertEqual(self.monitor.status()["state"], "STALLED")
                    self.assertEqual(self.monitor.status()["deadline_misses"], 1)
                    self.assertEqual(self.monitor.status()["deadline_misses"], 1)
                    self.assertEqual(self.monitor.run_once()["status"], "BUSY")
                    response = self.f.submit(self.client, proposal["proposal_id"])
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["order"]["status"], "REJECTED")
                    self.assertEqual(self.f.state(self.client)["fills"], [])
                    with self.assertRaisesRegex(RuntimeError, "providers must remain open"):
                        self.monitor.stop(timeout=0)
                    self.assertFalse(self.monitor.status()["entry_ready"])
                finally:
                    release.set()
                self.assertEqual(cycle.result(timeout=5)["status"], "FAILED")
        self.assertEqual(self.monitor.status()["deadline_misses"], 1)
        self.monitor.stop(timeout=0)
        self.assertEqual(self.monitor.status()["state"], "STOPPED")

    def test_latency_failure_is_not_a_success_and_stale_success_blocks_entries(self):
        self.ready()

        def late(**kwargs):
            self.mono.now += 10.5
            return {"positions": [], "orders": []}

        with patch.object(self.ctx.paper_position_manager, "manage", side_effect=late):
            self.assertEqual(self.monitor.run_once()["status"], "FAILED")
        self.assertEqual(self.monitor.status()["deadline_misses"], 1)
        self.assertEqual(self.monitor.status()["maximum_duration_seconds"], 10.5)
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.monitor.run_once()
        self.assertTrue(self.monitor.status()["entry_ready"])
        self.mono.now += 41
        self.assertFalse(self.monitor.status()["entry_ready"])

    def test_watchdog_detects_a_stall_without_waiting_for_the_worker(self):
        self.ready()
        self.monitor._active_since = self.mono()
        self.monitor._state = "MONITORING"
        self.mono.now += 11
        with patch.object(self.monitor._stop, "wait", side_effect=[False, True]):
            self.monitor._watch()
        self.assertEqual(self.monitor.status()["state"], "STALLED")
        self.assertEqual(self.monitor.status()["deadline_misses"], 1)
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.monitor._active_since = None

    def test_failed_startup_cycle_recovery_never_admits_new_entries(self):
        self.monitor.enable()
        self.monitor.run_once()
        loop = self.ctx.paper_loop
        with patch.object(loop.cycles.repository, "unfinished", return_value=[{"run_id": "interrupted"}]), \
                patch.object(loop.cycles, "recover", return_value={"status": "PARTIAL"}), \
                patch.object(loop._stop, "wait", side_effect=lambda _: loop._stop.set()):
            loop._run()
        self.assertFalse(self.monitor.recovery_complete)
        self.assertFalse(loop.status()["entries_enabled"])
        self.assertEqual(len(loop.status()["recovery_failures"]), 1)
        self.assertEqual(self.f.store.orders.all(), [])

    def test_reconciliation_failure_and_recovery_pending_block_scan_and_final_submission(self):
        proposal = self.f.propose(self.client)
        self.monitor.enable()
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.assertEqual(self.ctx.paper_loop.tick()["action"], "EXITS_ONLY")
        response = self.f.submit(self.client, proposal["proposal_id"])
        self.assertEqual(response.json()["order"]["status"], "REJECTED")
        self.assertEqual(self.f.state(self.client)["fills"], [])
        self.monitor.recovery_complete = True
        with patch.object(self.ctx.paper_position_manager, "manage", return_value={
                "orders": [{"status": "RECONCILE_FAILED"}], "positions": []}):
            self.assertEqual(self.monitor.run_once()["status"], "FAILED")
        self.assertEqual(self.ctx.paper_loop.tick()["action"], "EXITS_ONLY")

    def test_restart_restores_open_positions_and_pending_orders_before_entry_admission(self):
        proposal = self.f.propose(self.client)
        with patch.object(self.ctx.recovery._broker, "_apply_match"):
            self.assertEqual(self.f.submit(self.client, proposal["proposal_id"]).status_code, 200)
        restarted = self.f.context(auto_loop=True)
        monitor = restarted.paper_loop.protective_monitor
        monitor.enable()
        self.assertFalse(monitor.status()["entry_ready"])
        self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(restarted.paper.portfolio.positions()), 1)
        self.assertEqual(len(self.f.store.orders.all()), 1)
        monitor.recovery_complete = True
        self.assertTrue(monitor.status()["entry_ready"])
        self.f.clock.at += timedelta(minutes=1)
        self.f.data.price = 100
        self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(restarted.paper.portfolio.positions(), {})
        self.assertEqual(len(self.f.store.orders.all()), 2)
        again = self.f.context(auto_loop=True)
        self.assertEqual(again.paper.portfolio.positions(), {})
        self.assertGreater(again.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(self.ctx.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(len(self.f.store.paper_outcomes.recent()), 1)

    def test_missing_and_stale_prices_block_entries_without_inventing_fill(self):
        self.open_position()
        self.ready()
        before = self.f.state(self.client)
        with patch.object(self.ctx.paper_position_manager, "quotes", return_value=None):
            self.assertEqual(self.monitor.run_once()["status"], "FAILED")
        self.assertFalse(self.monitor.status()["entry_ready"])
        self.assertEqual(self.f.state(self.client), before)
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.f.clock.at += timedelta(minutes=10)
        with patch.object(self.ctx.paper_position_manager, "quotes",
                          return_value=(100., self.f.clock.at - timedelta(minutes=10))):
            self.assertEqual(self.monitor.run_once()["status"], "FAILED")
        self.assertEqual(self.f.state(self.client), before)

    def test_end_of_day_and_operator_pause_preserve_supervised_exits(self):
        self.open_position()
        self.ready()
        self.ctx.paper_loop.set_entries_enabled(False, operator="test", reason="pause")
        self.f.clock.at = self.f.clock.at.replace(hour=19, minute=52)
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(self.f.state(self.client)["positions"], [])
        self.assertEqual(self.f.store.orders.all()[-1]["trade_intent"], "SELL_TO_CLOSE")
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")
        self.assertEqual(len(self.f.store.orders.all()), 2)
        self.assertFalse(self.f.context(auto_loop=True).paper_loop.status()["operator_entries_enabled"])

    def test_entry_execution_lock_has_explicit_timeout(self):
        self.ready()
        held, release = Event(), Event()

        def hold():
            with self.ctx.paper.paper_strategy_change():
                held.set()
                if not release.wait(5):
                    raise RuntimeError("test execution lock was not released")

        self.monitor.settings = ProtectiveMonitorSettings(lock_timeout_seconds=.01)
        with ThreadPoolExecutor(max_workers=1) as worker:
            lock = worker.submit(hold)
            try:
                self.assertTrue(held.wait(5))
                result = self.monitor.run_once()
                self.assertEqual(result["status"], "FAILED")
                self.assertIn("PROTECTIVE_EXECUTION_LOCK_TIMEOUT", result["error"])
            finally:
                release.set()
            lock.result(timeout=5)
        self.assertEqual(self.monitor.run_once()["status"], "HEALTHY")

    def test_item_bounds_fail_closed_instead_of_reporting_unchecked_positions_as_success(self):
        self.open_position()
        self.monitor.settings = ProtectiveMonitorSettings(max_items=1)
        real = self.ctx.paper.portfolio.positions
        positions = real()
        positions["unknown:instrument"] = replace(
            next(iter(positions.values())), instrument_id="unknown:instrument")
        with patch.object(self.ctx.paper.portfolio, "positions", return_value=positions):
            self.monitor.enable()
            result = self.monitor.run_once()
            following = self.monitor.run_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["result"]["incomplete"])
        self.assertEqual(self.monitor.status()["positions_checked"], 1)
        self.assertEqual(following["result"]["positions"][0]["instrument_id"], "unknown:instrument")
        self.assertFalse(self.monitor.status()["entry_ready"])

    def test_live_rejected_and_automation_remains_disabled_by_default(self):
        positions = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.LIVE))
        with self.assertRaisesRegex(ValueError, "PAPER"):
            ProtectiveMonitor(positions=positions, markets=default_markets(), market="US",
                              flatten_minutes=10, settings=ProtectiveMonitorSettings(),
                              clock=self.f.clock)
        self.assertIsNone(self.f.context().paper_loop)
        for kwargs in ({"latency_budget_seconds": float("nan")}, {"max_items": 0},
                       {"lock_timeout_seconds": 11, "latency_budget_seconds": 10}):
            with self.assertRaises(ValueError):
                ProtectiveMonitorSettings(**kwargs)
        env = dict(fixtures.ENV, PAPER_AUTO_LOOP="true", PAPER_MONITOR_LATENCY_BUDGET_SECONDS="nan")
        with self.assertRaises(ConfigurationError):
            build_context(load_settings(env), env, store=self.f.store,
                          market_stats=fixtures.market_stats, market_data_provider=self.f.data,
                          research_analyst=self.ctx.candidate_assessment.analyst)
