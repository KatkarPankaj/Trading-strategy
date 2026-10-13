import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import default_markets
from stockmarket.core.paper_loop import PaperLoopSettings, PaperTradingLoop

# Monday 2026-10-05: the US regular session is 13:30-20:00 UTC (EDT).
IN_SESSION = datetime(2026, 10, 5, 15, 7, 31, tzinfo=timezone.utc)
NEAR_CLOSE = datetime(2026, 10, 5, 19, 52, tzinfo=timezone.utc)
CLOSED = datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


class _Gate:
    def __init__(self, reason=None):
        self.reason = reason

    @property
    def halted(self):
        return self.reason is not None

    def blocked_reason(self):
        return self.reason


class _Cycles:
    """Mirrors AutonomousPaperTradingService.run idempotency: one run per key."""

    def __init__(self, status="COMPLETE"):
        self.status = status
        self.runs = {}
        self.calls = []
        self.repository = SimpleNamespace(unfinished=Mock(return_value=[]))
        self.recover = Mock()

    def run(self, universe_id, *, idempotency_key, operator, top_n, as_of=None):
        self.calls.append(dict(universe_id=universe_id, key=idempotency_key,
                               operator=operator, as_of=as_of, top_n=top_n))
        run = self.runs.setdefault(idempotency_key, {
            "run_id": f"run-{len(self.runs) + 1}", "status": self.status})
        return run


def _loop(now, *, gate=None, cycles=None, mode=TradingMode.PAPER, **settings):
    positions = SimpleNamespace(
        trading=SimpleNamespace(mode=mode),
        flatten=Mock(return_value={"positions": []}),
        manage=Mock(return_value={"positions": []}),
    )
    clock = {"now": now}
    loop = PaperTradingLoop(
        cycles=cycles or _Cycles(), positions=positions, gate=gate or _Gate(),
        markets=default_markets(),
        settings=PaperLoopSettings(universe_id="US_ALL", market="US", **settings),
        clock=lambda: clock["now"])
    return loop, positions, clock


class PaperLoopTests(TestCase):
    def test_half_hour_scan_does_not_skip_monitoring_or_close_window(self):
        loop, positions, clock = _loop(
            NEAR_CLOSE.replace(minute=30), interval_minutes=30)
        self.assertEqual(loop.tick()["action"], "CYCLE")
        clock["now"] = NEAR_CLOSE.replace(minute=40)
        self.assertEqual(loop.tick()["action"], "MONITOR")
        clock["now"] = NEAR_CLOSE
        self.assertEqual(loop.tick()["action"], "END_OF_DAY_FLATTEN")
        self.assertEqual(len(loop.cycles.calls), 1)
        positions.flatten.assert_called_once_with(market="US")
        self.assertGreaterEqual(positions.manage.call_count, 2)

    def test_worker_wakes_at_monitor_cadence_not_only_at_scan_boundary(self):
        loop, positions, clock = _loop(
            NEAR_CLOSE.replace(minute=30), interval_minutes=30,
            monitor_interval_seconds=30)
        waits = []
        until = NEAR_CLOSE.replace(minute=53)

        def advance(seconds):
            waits.append(seconds)
            clock["now"] += timedelta(seconds=seconds)
            return False

        loop._stop = SimpleNamespace(is_set=lambda: clock["now"] >= until, wait=advance)
        loop._run()
        self.assertEqual(len(loop.cycles.calls), 1)
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), 30)
        self.assertGreater(positions.flatten.call_count, 0)
        self.assertGreater(positions.manage.call_count, 1)

    def test_successful_monitoring_does_not_erase_failed_scan_count(self):
        loop, positions, clock = _loop(IN_SESSION, max_consecutive_failures=2)
        loop.cycles.run = Mock(side_effect=RuntimeError("research unavailable"))
        self.assertEqual(loop.tick()["action"], "FAILED")
        clock["now"] += timedelta(seconds=30)
        self.assertEqual(loop.tick()["action"], "MONITOR")
        self.assertEqual(loop.status()["consecutive_failures"], 1)
        clock["now"] += timedelta(minutes=15)
        self.assertEqual(loop.tick()["action"], "FAILED")
        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        self.assertEqual(loop.tick()["action"], "EXITS_ONLY")

    def test_early_close_and_holiday_calendar_are_authoritative(self):
        early_close = datetime(2026, 11, 27, 17, 52, tzinfo=timezone.utc)
        loop, positions, _ = _loop(early_close, interval_minutes=30)
        self.assertEqual(loop.tick()["action"], "END_OF_DAY_FLATTEN")
        positions.flatten.assert_called_once()
        holiday = datetime(2026, 11, 26, 16, tzinfo=timezone.utc)
        loop, positions, _ = _loop(holiday)
        self.assertEqual(loop.tick()["action"], "MARKET_CLOSED")
        positions.manage.assert_not_called()

    def test_audited_pause_is_idempotent_keeps_exits_and_cannot_clear_failure_halt(self):
        loop, positions, clock = _loop(IN_SESSION)
        loop.record_control = Mock()
        paused = loop.set_entries_enabled(False, operator="reviewer", reason="pause")
        self.assertFalse(paused["entries_enabled"])
        loop.set_entries_enabled(False, operator="reviewer", reason="repeat")
        loop.record_control.assert_called_once_with(False, "reviewer", "pause")
        self.assertEqual(loop.tick()["action"], "EXITS_ONLY")
        positions.manage.assert_called_once()
        self.assertEqual(loop.cycles.calls, [])
        loop.set_entries_enabled(True, operator="reviewer", reason="resume")
        self.assertEqual(loop.tick()["action"], "CYCLE")
        loop._state = "STOPPED_AFTER_FAILURES"
        with self.assertRaisesRegex(ValueError, "resolve failures"):
            loop.set_entries_enabled(True, operator="reviewer", reason="override")
        clock["now"] = NEAR_CLOSE
        self.assertEqual(loop.tick()["action"], "END_OF_DAY_FLATTEN")

    def test_collection_failure_blocks_new_scan_but_preserves_exit_monitoring(self):
        loop, positions, _ = _loop(IN_SESSION, max_consecutive_failures=1)
        loop.reconcile_evidence = Mock(side_effect=ValueError("integrity failure"))
        self.assertEqual(loop.tick()["action"], "FAILED")
        self.assertEqual(loop.cycles.calls, [])
        positions.manage.assert_called_once()
        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        loop.tick()
        self.assertEqual(positions.manage.call_count, 2)
        self.assertFalse(loop._stop.is_set())

    def test_position_failure_blocks_scans_and_is_not_success_shaped(self):
        loop, positions, _ = _loop(IN_SESSION)
        positions.manage.return_value = {"positions": [
            {"instrument_id": "XNAS:AAPL", "status": "MONITOR_FAILED", "error": "STALE_QUOTE"}]}
        self.assertEqual(loop.tick()["action"], "FAILED")
        self.assertEqual(loop.cycles.calls, [])

    def test_concurrent_ticks_serialize_one_scan_per_slot(self):
        loop, positions, _ = _loop(IN_SESSION)
        entered, release = Event(), Event()
        original_run = loop.cycles.run

        def blocked_run(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture scan was not released")
            return original_run(*args, **kwargs)

        loop.cycles.run = blocked_run
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(loop.tick)
            self.assertTrue(entered.wait(5))
            second = executor.submit(loop.tick)
            release.set()
            self.assertEqual(first.result()["action"], "CYCLE")
            self.assertEqual(second.result()["action"], "MONITOR")
        self.assertEqual(len(loop.cycles.calls), 1)

    def test_shutdown_timeout_is_explicit_and_does_not_report_stopped(self):
        loop, _, _ = _loop(IN_SESSION)
        loop._thread = Mock()
        loop._thread.is_alive.return_value = True
        loop._state = "RUNNING"
        with self.assertRaisesRegex(RuntimeError, "providers must remain open"):
            loop.stop(timeout=.01)
        self.assertEqual(loop.status()["state"], "STOPPING")

    def test_closed_market_and_weekend_do_nothing(self):
        for now in (CLOSED, SATURDAY):
            loop, positions, _ = _loop(now)
            with self.subTest(now=now):
                self.assertEqual(loop.tick()["action"], "MARKET_CLOSED")
                self.assertEqual(loop.cycles.calls, [])
                positions.flatten.assert_not_called()
                positions.manage.assert_not_called()

    def test_cycle_uses_a_deterministic_bucket_key_and_is_idempotent(self):
        loop, _, clock = _loop(IN_SESSION, top_n=2)

        first = loop.tick()
        clock["now"] = IN_SESSION.replace(minute=14)
        repeat = loop.tick()

        self.assertEqual(first["action"], "CYCLE")
        self.assertEqual(first["idempotency_key"], "loop-US_ALL-20261005T1500")
        self.assertEqual(repeat["run_id"], first["run_id"])
        self.assertEqual(repeat["action"], "MONITOR")
        self.assertEqual(len(loop.cycles.calls), 1)
        call = loop.cycles.calls[0]
        self.assertIsNone(call["as_of"])
        self.assertEqual((call["top_n"], call["operator"]), (2, "paper-loop"))
        self.assertEqual(len(loop.cycles.runs), 1)

    def test_eod_window_flattens_instead_of_opening_positions(self):
        loop, positions, _ = _loop(NEAR_CLOSE)

        result = loop.tick()

        self.assertEqual(result["action"], "END_OF_DAY_FLATTEN")
        positions.flatten.assert_called_once_with(market="US")
        self.assertEqual(loop.cycles.calls, [])

    def test_halted_gate_runs_exits_only(self):
        loop, positions, _ = _loop(IN_SESSION, gate=_Gate("KILL_SWITCH: drawdown"))

        result = loop.tick()

        self.assertEqual(result["action"], "EXITS_ONLY")
        self.assertIn("KILL_SWITCH", result["reason"])
        positions.manage.assert_called_once_with()
        self.assertEqual(loop.cycles.calls, [])

    def test_consecutive_failures_stop_the_loop_and_success_resets_the_count(self):
        loop, _, clock = _loop(IN_SESSION, cycles=_Cycles(status="FAILED"),
                               max_consecutive_failures=2)
        self.assertEqual(loop.tick()["action"], "FAILED")
        self.assertEqual(loop.status()["consecutive_failures"], 1)
        loop.cycles.status = "COMPLETE"
        clock["now"] = IN_SESSION.replace(minute=30)
        self.assertEqual(loop.tick()["consecutive_failures"], 0)

        loop.cycles.run = Mock(side_effect=RuntimeError("RateLimited"))
        clock["now"] = IN_SESSION.replace(hour=16, minute=0)
        loop.tick()
        clock["now"] = IN_SESSION.replace(hour=16, minute=15)
        loop.tick()

        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        self.assertFalse(loop._stop.is_set())

    def test_unfinished_runs_are_recovered_and_recovery_failures_counted(self):
        cycles = _Cycles()
        cycles.repository.unfinished.return_value = [{"run_id": "r1"}, {"run_id": "r2"}]
        cycles.recover.side_effect = [None, RuntimeError("broken")]
        loop, _, _ = _loop(IN_SESSION, cycles=cycles)

        self.assertEqual(loop.recover_unfinished(), ["r1"])
        self.assertEqual(loop.status()["consecutive_failures"], 1)

    def test_requires_paper_mode_known_market_and_valid_settings(self):
        with self.assertRaises(ValueError):
            _loop(IN_SESSION, mode=TradingMode.LIVE)
        with self.assertRaises(Exception):
            PaperTradingLoop(
                cycles=_Cycles(), positions=SimpleNamespace(
                    trading=SimpleNamespace(mode=TradingMode.PAPER)),
                gate=_Gate(), markets=default_markets(),
                settings=PaperLoopSettings(universe_id="X", market="ZZ"))
        for bad in ({"interval_minutes": 0}, {"top_n": True}, {"eod_flatten_minutes": 500}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PaperLoopSettings(universe_id="US_ALL", market="US", **bad)

    def test_next_wait_aligns_to_the_bucket_boundary(self):
        loop, _, _ = _loop(IN_SESSION)
        self.assertAlmostEqual(loop.seconds_until_next(IN_SESSION), 7 * 60 + 29 - 0.0, places=0)

    def test_partial_cycles_exhaust_failure_limit_and_stopped_ticks_do_not_submit(self):
        loop, positions, clock = _loop(
            IN_SESSION, cycles=_Cycles(status="PARTIAL"), max_consecutive_failures=2)
        loop.tick()
        clock["now"] = IN_SESSION.replace(minute=30)
        loop.tick()
        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        calls = len(loop.cycles.calls)
        self.assertEqual(loop.tick()["action"], "EXITS_ONLY")
        self.assertEqual(len(loop.cycles.calls), calls)
        self.assertGreaterEqual(positions.manage.call_count, 1)

    def test_recovery_failure_threshold_blocks_first_entry_tick(self):
        cycles = _Cycles()
        cycles.repository.unfinished.return_value = [{"run_id": "r1"}, {"run_id": "r2"}]
        cycles.recover.side_effect = RuntimeError("IdempotencyConflict")
        loop, _, _ = _loop(IN_SESSION, cycles=cycles, max_consecutive_failures=2)
        loop.recover_unfinished()
        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        self.assertEqual(loop.tick()["action"], "EXITS_ONLY")
        self.assertEqual(cycles.calls, [])
        self.assertEqual(len(loop.status()["recovery_failures"]), 2)

    def test_naive_loop_clock_is_rejected_without_running_a_cycle(self):
        loop, _, _ = _loop(IN_SESSION.replace(tzinfo=None))
        self.assertEqual(loop.tick()["action"], "FAILED")
        self.assertEqual(loop.cycles.calls, [])

    def test_background_wait_rechecks_the_wall_clock_after_host_sleep(self):
        loop, _, clock = _loop(IN_SESSION)
        loop.wait_poll_seconds = 0.01
        loop.start()
        self.addCleanup(loop.stop)
        deadline = time.monotonic() + 5
        while loop.status()["ticks"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        # The wall clock jumps past the next slot, as after Windows Modern Standby;
        # a single 7.5-minute timer would not wake for the 15:15 slot in time.
        clock["now"] = IN_SESSION.replace(minute=20)
        while loop.status()["ticks"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        loop.stop()

        self.assertEqual(loop.status()["ticks"], 2)
        self.assertEqual([c["key"] for c in loop.cycles.calls],
                         ["loop-US_ALL-20261005T1500", "loop-US_ALL-20261005T1515"])
