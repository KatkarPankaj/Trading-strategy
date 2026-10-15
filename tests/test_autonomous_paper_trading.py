from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase

from stockmarket.core.autonomous_paper_trading import (
    AutonomousPaperCycleError,
    AutonomousPaperTradingService,
)
from stockmarket.core.executors import TradingMode
from stockmarket.core.models import OrderStatus
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate


NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = "XNAS:AAPL"


class _Research:
    def run(self, universe_id, *, idempotency_key, mode, as_of, top_n):
        return {
            "run_id": "research-run-1",
            "status": "COMPLETE",
            "candidates": [{
                "instrument_id": INSTRUMENT_ID,
                "stage": "STRATEGY_SELECTED",
            }],
        }


class _Signals:
    def generate(self, run_id, candidate_id, *, evaluation_as_of):
        return {
            "status": "SIGNAL_GENERATED",
            "signal_id": "signal-1",
        }


class _Proposals:
    def evaluate(self, run_id, candidate_id, *, evaluation_as_of):
        return {
            "status": "RISK_APPROVED",
            "trade_proposal": {"proposal_id": "proposal-1"},
        }


class _Execution:
    def __init__(self, *, fail_first=False):
        self.trading = SimpleNamespace(mode=TradingMode.PAPER)
        self.calls = []
        self.fail_first = fail_first

    def submit(self, proposal_id, *, operator, sizing_mode, quantity):
        self.calls.append((proposal_id, operator, sizing_mode, quantity))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("simulated interruption")
        return {
            "order": SimpleNamespace(
                client_order_id="paper-client-1",
                broker_order_id="PAPER-1",
                status=OrderStatus.FILLED,
                filled_quantity=5,
                average_fill_price=100.0,
                error=None,
            ),
        }


class _Positions:
    def manage(self):
        return {"trading_mode": "PAPER", "positions": []}


class _Gate:
    halted = False

    def blocked_reason(self):
        return None


def _service(
    store, *, execution=None, positions=None, gate=None, research=None,
    signals=None, proposals=None, clock=lambda: NOW,
):
    return AutonomousPaperTradingService(
        repository=store.autonomous_paper_cycles,
        research=research or _Research(),
        signals=signals or _Signals(),
        proposals=proposals or _Proposals(),
        execution=execution or _Execution(),
        positions=positions or _Positions(),
        gate=gate or _Gate(),
        max_candidates=3,
        clock=clock,
        heartbeat_interval=60,
    )


class AutonomousPaperTradingTests(TestCase):
    def setUp(self):
        self.database = SQLiteDatabase()
        migrate(self.database)
        self.store = Store(self.database)
        self.addCleanup(self.database.close)

    def test_cycle_is_paper_only_bounded_and_idempotent(self):
        execution = _Execution()
        service = _service(self.store, execution=execution)

        result = service.run(
            "us-equities",
            idempotency_key="cycle-001",
            operator="operator",
            as_of=NOW,
            top_n=1,
        )
        replay = service.run(
            "us-equities",
            idempotency_key="cycle-001",
            operator="operator",
            as_of=NOW,
            top_n=1,
        )

        self.assertEqual(result["mode"], "PAPER")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["candidates"][0]["status"], "COMPLETE")
        self.assertEqual(replay["run_id"], result["run_id"])
        self.assertEqual(len(execution.calls), 1)
        self.assertEqual(execution.calls[0][2:], ("AUTOMATIC_SIZING", None))
        self.assertEqual(self.store.autonomous_paper_cycles.unfinished(), [])
        trace = result["candidates"][0]["payload"]["decision_trace"]
        self.assertEqual(trace["final_status"], "FILLED")
        self.assertEqual(trace["client_order_id"], "paper-client-1")
        self.assertIsNone(trace["rejection_code"])

    def test_research_selection_error_is_retained_in_candidate_trace(self):
        class RejectedResearch(_Research):
            def run(self, *args, **kwargs):
                result = super().run(*args, **kwargs)
                result["candidates"][0].update({
                    "stage": "REJECTED",
                    "opportunity": {"assessment": {
                        "status": "NO_VALID_STRATEGY",
                        "error": "STRATEGY_SELECTION_REJECTED: unknown strategy candidate: orb_vwap v1.0.0",
                    }},
                })
                return result
        execution = _Execution()
        service = _service(self.store, execution=execution, research=RejectedResearch())
        result = service.run("US_ALL", idempotency_key="bad-strategy", operator="fixture")
        candidate = result["candidates"][0]
        trace = candidate["payload"]["decision_trace"]
        self.assertEqual(trace["final_status"], "REJECTED")
        self.assertEqual(trace["rejection_code"], "NO_VALID_STRATEGY")
        self.assertIn("orb_vwap v1.0.0", trace["reason"])
        self.assertEqual(execution.calls, [])

    def test_submitted_order_is_not_reported_as_rejected_or_filled(self):
        class PendingExecution(_Execution):
            def submit(self, *args, **kwargs):
                result = super().submit(*args, **kwargs)
                result["order"].status = OrderStatus.SUBMITTED
                result["order"].filled_quantity = 0
                return result
        service = _service(self.store, execution=PendingExecution())
        result = service.run("US_ALL", idempotency_key="submitted", operator="fixture")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["status"], "COMPLETE")
        self.assertEqual(candidate["payload"]["decision_trace"]["final_status"], "SUBMITTED")

    def test_order_states_are_persisted_and_rendered_without_conflation(self):
        from stockmarket.dashboard.views import cycle_candidates

        for state, decision in (
            (OrderStatus.SUBMITTED, "SUBMITTED"),
            (OrderStatus.PARTIALLY_FILLED, "SUBMITTED"),
            (OrderStatus.FILLED, "FILLED"),
            (OrderStatus.REJECTED, "REJECTED"),
            (OrderStatus.CANCELLED, "CANCELLED"),
        ):
            with self.subTest(state=state):
                class StateExecution(_Execution):
                    def submit(self, *args, **kwargs):
                        result = super().submit(*args, **kwargs)
                        result["order"].status = state
                        result["order"].filled_quantity = (
                            5 if state == OrderStatus.FILLED else
                            2 if state == OrderStatus.PARTIALLY_FILLED else 0)
                        result["order"].error = (
                            "INSUFFICIENT_CASH: fixture rejection"
                            if state == OrderStatus.REJECTED else None)
                        return result

                service = _service(self.store, execution=StateExecution())
                result = service.run(
                    "US_ALL", idempotency_key=f"state-{state.value}", operator="fixture")
                persisted = self.store.autonomous_paper_cycles.get_candidate(
                    result["run_id"], INSTRUMENT_ID)
                trace = persisted["payload"]["decision_trace"]
                self.assertEqual(trace["final_status"], decision)
                self.assertEqual(persisted["payload"]["order"]["status"], state.value)
                shown = cycle_candidates([persisted]).iloc[0]
                self.assertEqual(shown["decision"], decision)
                self.assertEqual(shown["order_status"], state.value)
                if state == OrderStatus.CANCELLED:
                    self.assertIsNone(trace["rejection_code"])
                    self.assertIn("cancelled", trace["reason"].lower())
                if state == OrderStatus.REJECTED:
                    self.assertEqual(trace["rejection_code"], "INSUFFICIENT_CASH")

    def test_obsolete_configuration_recovery_terminates_without_retrying_entries(self):
        from stockmarket.core.autonomous_research import IdempotencyConflict

        class ChangedResearch(_Research):
            def run(self, *args, **kwargs):
                raise IdempotencyConflict("configuration changed")

        execution = _Execution()
        service = _service(self.store, research=ChangedResearch(), execution=execution)
        with self.assertRaises(AutonomousPaperCycleError):
            service.run("us-equities", idempotency_key="old-config",
                        operator="operator", top_n=1)
        failed = service.get_run(self.store.autonomous_paper_cycles.get_by_key("old-config")["run_id"])
        self.assertEqual(failed["status"], "PARTIAL")
        service.clock = lambda: NOW + timedelta(minutes=16)
        recovered = service.recover(failed["run_id"])
        self.assertEqual((recovered["status"], recovered["stage"]),
                         ("FAILED", "RECOVERY_OBSOLETE"))
        self.assertEqual(execution.calls, [])
        self.assertEqual(self.store.autonomous_paper_cycles.unfinished(), [])
        self.assertEqual(recovered["payload"]["failure"], failed["payload"]["failure"])
        self.assertFalse(recovered["payload"]["recovery_disposition"]["entries_retried"])
        self.assertGreater(len(recovered["events"]), len(failed["events"]))

    def test_expired_cycle_cannot_submit_before_the_next_heartbeat(self):
        state = {"now": NOW}

        class SlowResearch(_Research):
            def run(self, *args, **kwargs):
                state["now"] += timedelta(minutes=16)
                return super().run(*args, **kwargs)

        execution = _Execution()
        service = _service(self.store, research=SlowResearch(), execution=execution,
                           clock=lambda: state["now"])
        with self.assertRaisesRegex(AutonomousPaperCycleError, "lease was lost"):
            service.run("us-equities", idempotency_key="expired-during-research",
                        operator="operator", top_n=1)
        self.assertEqual(execution.calls, [])
        run = self.store.autonomous_paper_cycles.get_by_key("expired-during-research")
        owner = self.database.query(
            "SELECT owner_token FROM autonomous_paper_cycle_locks WHERE run_id = ?",
            (run["run_id"],))[0]["owner_token"]
        self.assertFalse(self.store.autonomous_paper_cycles.renew_lock(
            run["run_id"], owner_token=owner,
            lease_until=state["now"] + timedelta(minutes=15), now=state["now"]))

    def test_candidate_evaluation_uses_time_after_research_evidence_is_created(self):
        clock_state = {"now": NOW}
        evaluation_times = []

        class AdvancingResearch(_Research):
            def run(self, *args, **kwargs):
                result = super().run(*args, **kwargs)
                clock_state["now"] = NOW + timedelta(seconds=5)
                return result

        class RecordingSignals(_Signals):
            def generate(self, run_id, candidate_id, *, evaluation_as_of):
                evaluation_times.append(evaluation_as_of)
                return super().generate(
                    run_id, candidate_id, evaluation_as_of=evaluation_as_of)

        service = _service(
            self.store,
            research=AdvancingResearch(),
            signals=RecordingSignals(),
            clock=lambda: clock_state["now"],
        )

        result = service.run(
            "us-equities",
            idempotency_key="cycle-research-time",
            operator="operator",
            as_of=NOW,
            top_n=1,
        )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(evaluation_times, [NOW + timedelta(seconds=5)])

    def test_omitted_as_of_uses_current_time_and_preserves_retry_timestamp(self):
        clock_state = {"now": NOW + timedelta(minutes=18)}
        execution = _Execution()
        service = _service(
            self.store, execution=execution, clock=lambda: clock_state["now"])
        first = service.run(
            "us-equities", idempotency_key="loop-delayed-slot",
            operator="operator", top_n=1)
        clock_state["now"] += timedelta(minutes=2)
        replay = service.run(
            "us-equities", idempotency_key="loop-delayed-slot",
            operator="operator", top_n=1)

        self.assertEqual(first["as_of"], (NOW + timedelta(minutes=18)).isoformat())
        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(replay["as_of"], first["as_of"])
        self.assertEqual(len(execution.calls), 1)

    def test_failed_candidate_can_resume_from_checkpoint(self):
        execution = _Execution(fail_first=True)
        service = _service(self.store, execution=execution)

        first = service.run(
            "us-equities",
            idempotency_key="cycle-recover",
            operator="operator",
            as_of=NOW,
            top_n=1,
        )
        self.assertEqual(first["status"], "PARTIAL")
        self.assertEqual(first["candidates"][0]["status"], "FAILED")

        recovered = service.recover(first["run_id"])

        self.assertEqual(recovered["status"], "COMPLETE", recovered)
        self.assertEqual(recovered["candidates"][0]["status"], "COMPLETE")
        self.assertEqual(len(execution.calls), 2)
        payload = recovered["candidates"][0]["payload"]
        self.assertNotIn("error", payload)
        self.assertEqual(payload["decision_trace"]["final_status"], "FILLED")
        self.assertIsNone(payload["decision_trace"]["rejection_code"])

    def test_cycle_scope_lock_prevents_overlapping_runs(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingPositions:
            def manage(self):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test barrier timed out")
                return {"positions": []}

        first_service = _service(
            self.store, positions=BlockingPositions())
        second_service = _service(self.store)
        errors = []

        def run_first():
            try:
                first_service.run(
                    "us-equities",
                    idempotency_key="cycle-lock-a",
                    operator="operator",
                    as_of=NOW,
                    top_n=1,
                )
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            with self.assertRaises(AutonomousPaperCycleError):
                second_service.run(
                    "us-equities",
                    idempotency_key="cycle-lock-b",
                    operator="operator",
                    as_of=NOW,
                    top_n=1,
                )
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_recovery_requires_expired_lease(self):
        service = _service(self.store)
        run_id = "interrupted-cycle"
        key = "interrupted-key"
        payload = {
            "request": {
                "universe_id": "us-equities",
                "idempotency_key": key,
                "operator": "operator",
                "mode": "PAPER",
                "as_of": NOW.isoformat(),
                "top_n": 1,
            },
        }
        self.store.autonomous_paper_cycles.create(
            run_id=run_id,
            idempotency_key=key,
            request_hash=service._request_hash(
                "us-equities", NOW, 1, "operator"),
            universe_id="us-equities",
            market_scope="PAPER",
            account_id="PAPER",
            as_of=NOW,
            payload=payload,
            owner_token="previous-process",
            lease_until=NOW - timedelta(seconds=1),
        )

        recovered = service.recover(run_id)

        self.assertEqual(recovered["status"], "COMPLETE")
        self.assertEqual(recovered["mode"], "PAPER")

    def test_halted_gate_still_manages_positions_but_skips_entries(self):
        class HaltedGate:
            halted = True

            def blocked_reason(self):
                return "operator halt"

        class PositionManager:
            def __init__(self):
                self.calls = 0

            def manage(self):
                self.calls += 1
                return {"positions": [{"status": "EXIT_PENDING"}]}

        positions = PositionManager()
        execution = _Execution()
        service = _service(
            self.store, execution=execution, positions=positions, gate=HaltedGate())
        result = service.run(
            "us-equities",
            idempotency_key="cycle-halted",
            operator="operator",
            as_of=NOW,
            top_n=1,
        )

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(positions.calls, 1)
        self.assertEqual(execution.calls, [])
        self.assertEqual(result["payload"]["halt_reason"], "operator halt")
