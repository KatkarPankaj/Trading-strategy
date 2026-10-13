"""Offline execution stress workload. Run with PYTHONPATH=src;tests."""
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, local
from time import perf_counter
from unittest import TestCase, main
from unittest.mock import patch

import test_paper_end_to_end as fixtures
from stockmarket.core.models import OrderSide, OrderType
from stockmarket.core.trading_service import OrderTicket


def distribution(values):
    if not values:
        return {"samples": 0}
    values = sorted(values)
    return {"samples": len(values), "p50": values[math.ceil(len(values) * .5) - 1],
            "p95": values[math.ceil(len(values) * .95) - 1], "worst": values[-1]}


class MeasuredLock:
    def __init__(self, lock):
        self.lock, self.thread = lock, local()
        self.waits, self.durations = [], []

    def acquire(self, *args, **kwargs):
        started = perf_counter()
        acquired = self.lock.acquire(*args, **kwargs)
        self.waits.append(perf_counter() - started)
        if acquired:
            depth = getattr(self.thread, "depth", 0)
            if depth == 0:
                self.thread.started = perf_counter()
            self.thread.depth = depth + 1
        return acquired

    def release(self):
        self.thread.depth -= 1
        if self.thread.depth == 0:
            self.durations.append(perf_counter() - self.thread.started)
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class IsolationStress(TestCase):
    def test_controlled_history_and_database_contention(self):
        f = fixtures.PaperEndToEndTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        ctx = f.context(auto_loop=True)
        measured_lock = MeasuredLock(ctx.paper._submit_lock)
        ctx.paper._submit_lock = measured_lock
        client = f.client(ctx)
        proposal = f.propose(client)
        self.assertEqual(f.submit(client, proposal["proposal_id"]).status_code, 200)
        monitor = ctx.paper_loop.protective_monitor
        monitor.monotonic = perf_counter
        monitor.settings = replace(monitor.settings, lock_timeout_seconds=.01)
        monitor.enable()
        monitor.recovery_complete = True
        self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        periods, quote_latencies, database_waits = [], [], []
        rejected = 0
        failed_checks = 0
        original_quote = ctx.paper_position_manager.quotes

        def timed_quote(iid):
            started = perf_counter()
            try:
                return original_quote(iid)
            finally:
                quote_latencies.append(perf_counter() - started)

        with patch.object(ctx.paper_position_manager, "quotes", side_effect=timed_quote):
            for n in range(20):
                entered, release = Event(), Event()

                def history(iid):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("controlled history not released")
                    return fixtures.market_stats(iid)

                ticket = OrderTicket(
                    f.instrument_id, OrderSide.BUY, 1, OrderType.MARKET, "orb_vwap",
                    client_order_id=f"stress-{n}", stop_loss=101., take_profit=106.)
                with patch.object(ctx.paper, "_stats", side_effect=history):
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        entry = worker.submit(ctx.paper.submit, ticket)
                        try:
                            self.assertTrue(entered.wait(5))
                            started = perf_counter()
                            result = monitor.run_once()
                            periods.append(perf_counter() - started)
                            self.assertFalse(entry.done())
                            expected = "HEALTHY" if hasattr(ctx.paper, "prepare_entry") else "FAILED"
                            self.assertEqual(result["status"], expected)
                            failed_checks += int(result["status"] == "FAILED")
                            # Independently change final risk conditions while preparation is blocked.
                            ctx.gate.halt("STRESS", "controlled risk change")
                        finally:
                            release.set()
                        final = entry.result(timeout=5)
                        self.assertEqual(final.risk.status.value, "REJECTED")
                        rejected += 1
                ctx.gate.clear("STRESS")
                self.assertEqual(monitor.run_once()["status"], "HEALTHY")
            # Controlled DB ownership: timeout admission, then verify full recovery.
            if hasattr(f.store.db, "lock_timeout"):
                f.store.db.lock_timeout = .01
                owned, release = Event(), Event()

                def transaction():
                    with f.store.db.transaction():
                        owned.set()
                        if not release.wait(5):
                            raise RuntimeError("controlled transaction not released")

                with ThreadPoolExecutor(max_workers=1) as worker:
                    holding = worker.submit(transaction)
                    try:
                        self.assertTrue(owned.wait(5))
                        started = perf_counter()
                        with self.assertRaisesRegex(Exception, "DATABASE_LOCK_TIMEOUT"):
                            f.store.db.query("SELECT 1")
                        database_waits.append(perf_counter() - started)
                    finally:
                        release.set()
                    holding.result(timeout=5)
                self.assertEqual(monitor.run_once()["status"], "HEALTHY")
        schedule_delays = []
        scheduled_done = Event()
        original_run = monitor.run_once
        scheduled_count = 0

        def observe_schedule():
            nonlocal scheduled_count
            result = original_run()
            scheduled_count += 1
            delay = monitor.status().get("scheduling_delay_seconds")
            if delay is not None:
                schedule_delays.append(delay)
            if scheduled_count >= 20:
                monitor.request_stop()
                scheduled_done.set()
            return result

        monitor.settings = replace(monitor.settings, interval_seconds=.001)
        with patch.object(monitor, "run_once", side_effect=observe_schedule):
            monitor.start()
            try:
                self.assertTrue(scheduled_done.wait(5))
            finally:
                monitor.stop(timeout=5)
        self.assertEqual(len(f.store.fills.all()), 1)
        self.assertEqual(len(f.store.orders.all()), rejected + 1)
        db_timings = list(getattr(f.store.db, "timings", []))
        print("EXECUTION_ISOLATION_STRESS " + json.dumps({
            "implementation": "prepared" if hasattr(ctx.paper, "prepare_entry") else "baseline",
            "monitor_call_seconds": distribution(periods),
            "quote_seconds": distribution(quote_latencies),
            "execution_lock_wait_seconds": distribution(measured_lock.waits),
            "critical_seconds": distribution(measured_lock.durations),
            "database_wait_seconds": distribution(database_waits),
            "database_transaction_seconds": distribution(
                [t["duration_seconds"] for t in db_timings if t["operation"] == "transaction"]),
            "rejected_entries": rejected, "deadline_misses": monitor.status()["deadline_misses"],
            "failed_monitor_checks": failed_checks,
            "duplicate_fills": 0, "scheduling_delay_seconds": distribution(schedule_delays),
            "limits": "20 event-held history calls; 20 scheduled cycles; one post-change DB timeout; "
                      "baseline DB and schedule telemetry unavailable; fixtures; no vendor SLA"
        }, sort_keys=True))


if __name__ == "__main__":
    main()
