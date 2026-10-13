import time
from datetime import datetime, timezone
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

    def __init__(self, status="COMPLETED"):
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
        loop.cycles.status = "COMPLETED"
        clock["now"] = IN_SESSION.replace(minute=30)
        self.assertEqual(loop.tick()["consecutive_failures"], 0)

        loop.cycles.run = Mock(side_effect=RuntimeError("RateLimited"))
        loop.tick()
        loop.tick()

        self.assertEqual(loop.status()["state"], "STOPPED_AFTER_FAILURES")
        self.assertTrue(loop._stop.is_set())

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
