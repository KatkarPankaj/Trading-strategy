from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from stockmarket.api.bootstrap import _discovery_from_env
from stockmarket.core.adaptive_discovery import (
    AdaptiveDiscoverySettings, parse_discovery_settings, plan_rotation, protected_instruments)
from stockmarket.core.autonomous_paper_trading import (
    AutonomousPaperCycleError, AutonomousPaperTradingService)
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.persistence.repositories import ScannerRunRepository
from stockmarket.core.scanner import ScanMode
from stockmarket.core.settings import ConfigurationError

from test_autonomous_paper_trading import NOW, _Execution, _Gate, _Positions, _Signals
from test_market_scanner import scanner_fixture

SETTINGS = AdaptiveDiscoverySettings()
AAPL, MSFT, NVDA = "XNAS:AAPL", "XNAS:MSFT", "XNAS:NVDA"


def hold(instrument):
    return {"candidate_id": instrument, "status": "NON_ACTIONABLE", "payload": {}}


def research_rejected(instrument, status):
    return {"candidate_id": instrument, "status": "RESEARCH_REJECTED",
            "payload": {"research_assessment": {"status": status}}}


def run(minutes_ago, *candidates, discovery=None):
    payload = {"discovery": discovery} if discovery else {}
    return {"run_id": f"run-{minutes_ago}", "as_of": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "payload": payload, "candidates": list(candidates)}


def decision(plan, instrument):
    return next(item for item in plan["decisions"] if item["instrument_id"] == instrument)


class RotationPolicyTests(TestCase):
    def test_hold_threshold_rotates_and_shorter_hold_is_retained(self):
        history = [run(35, hold(AAPL), hold(MSFT)), run(20, hold(AAPL), hold(MSFT))]
        plan = plan_rotation(history, protected=set(), now=NOW, settings=SETTINGS)
        self.assertEqual(plan["deprioritized"], [AAPL, MSFT])
        self.assertEqual(decision(plan, AAPL)["reason_code"], "HOLD_THRESHOLD_EXCEEDED")
        self.assertEqual(decision(plan, AAPL)["hold_minutes"], 35.0)
        longer = plan_rotation(history, protected=set(), now=NOW,
                               settings=AdaptiveDiscoverySettings(hold_threshold_minutes=60))
        self.assertEqual(longer["deprioritized"], [])
        self.assertEqual(decision(longer, AAPL)["reason_code"], "HOLD_BELOW_THRESHOLD")

    def test_non_hold_outcomes_and_provider_or_ai_errors_reset_the_streak(self):
        history = [run(50, hold(AAPL), hold(MSFT)),
                   run(30, {"candidate_id": AAPL, "status": "SIGNAL_REJECTED", "payload": {}},
                       research_rejected(MSFT, "AI_INVALID")),
                   run(10, hold(AAPL), hold(MSFT))]
        plan = plan_rotation(history, protected=set(), now=NOW, settings=SETTINGS)
        self.assertEqual(plan["deprioritized"], [])
        self.assertEqual({item["reason_code"] for item in plan["decisions"]}, {"HOLD_BELOW_THRESHOLD"})

    def test_no_validated_strategy_abstention_is_a_discovery_trigger_only(self):
        history = [run(40, research_rejected(AAPL, "NO_VALIDATED_STRATEGY")),
                   run(5, research_rejected(AAPL, "NO_VALIDATED_STRATEGY"))]
        plan = plan_rotation(history, protected=set(), now=NOW, settings=SETTINGS)
        self.assertEqual(plan["deprioritized"], [AAPL])
        self.assertEqual(decision(plan, AAPL)["hold_code"], "HOLD_NO_VALIDATED_STRATEGY")

    def test_positions_and_pending_orders_are_retained_and_incomplete_monitoring_skips(self):
        history = [run(45, hold(AAPL), hold(MSFT), hold(NVDA))]
        protected = protected_instruments({
            "positions": [{"instrument_id": AAPL, "status": "MONITORED"}],
            "orders": [{"instrument_id": MSFT, "status": "SUBMITTED"}]})
        plan = plan_rotation(history, protected=protected, now=NOW, settings=SETTINGS)
        self.assertEqual(plan["deprioritized"], [NVDA])
        for instrument in (AAPL, MSFT):
            self.assertEqual(decision(plan, instrument)["reason_code"], "RETAINED_ACTIVE_POSITION_OR_ORDER")
        busy = protected_instruments({"incomplete": True, "error": "MONITORING_BUSY", "positions": []})
        skipped = plan_rotation(history, protected=busy, now=NOW, settings=SETTINGS)
        self.assertEqual((skipped["deprioritized"], skipped["skipped"]),
                         ([], "DISCOVERY_SKIPPED_MONITORING_INCOMPLETE"))

    def test_capacity_limits_rotation_and_duplicates_are_suppressed(self):
        history = [run(60, hold(MSFT), hold(MSFT)), run(45, hold(AAPL), hold(MSFT))]
        plan = plan_rotation(history, protected=set(), now=NOW,
                             settings=AdaptiveDiscoverySettings(max_rotated=1))
        self.assertEqual(plan["deprioritized"], [MSFT])
        self.assertEqual(decision(plan, AAPL)["reason_code"], "ROTATION_CAPACITY_REACHED")
        self.assertEqual([item["instrument_id"] for item in plan["decisions"]].count(MSFT), 1)

    def test_cooldown_persists_then_old_evidence_cannot_retrigger(self):
        rotated = {"decisions": [{"instrument_id": AAPL, "action": "ROTATED",
                                  "rotated_at": (NOW - timedelta(minutes=30)).isoformat()}]}
        history = [run(70, hold(AAPL)), run(30, discovery=rotated)]
        active = plan_rotation(history, protected=set(), now=NOW, settings=SETTINGS)
        self.assertEqual(active["deprioritized"], [AAPL])
        self.assertEqual(decision(active, AAPL)["reason_code"], "COOLDOWN_ACTIVE")
        later = NOW + timedelta(minutes=31)
        expired = plan_rotation(history, protected=set(), now=later, settings=SETTINGS)
        self.assertEqual(expired["deprioritized"], [])

    def test_settings_are_opt_in_configurable_and_validated(self):
        self.assertIsNone(parse_discovery_settings({}))
        custom = parse_discovery_settings({
            "PAPER_DISCOVERY_ENABLED": "true", "PAPER_DISCOVERY_HOLD_MINUTES": "45",
            "PAPER_DISCOVERY_COOLDOWN_MINUTES": "90", "PAPER_DISCOVERY_MAX_ROTATED": "5"})
        self.assertEqual((custom.hold_threshold_minutes, custom.cooldown_minutes, custom.max_rotated),
                         (45, 90, 5))
        for bad in ({"PAPER_DISCOVERY_ENABLED": "yes"},
                    {"PAPER_DISCOVERY_ENABLED": "true", "PAPER_DISCOVERY_HOLD_MINUTES": "1"}):
            with self.assertRaises(ConfigurationError):
                _discovery_from_env(bad)


class ScannerRankingTests(TestCase):
    def test_deprioritized_instrument_ranks_last_but_is_never_discarded(self):
        db = SQLiteDatabase()
        migrate(db)
        self.addCleanup(db.close)
        scanner, _ = scanner_fixture(repository=ScannerRunRepository(db))
        baseline = scanner.scan("TST_LIQUID", ScanMode.RESEARCH, top_n=1)
        first = baseline.candidates[0].instrument_id
        rotated = scanner.scan("TST_LIQUID", ScanMode.RESEARCH, top_n=1, deprioritized=(first,))
        self.assertNotEqual(rotated.candidates[0].instrument_id, first)
        self.assertEqual(rotated.accepted_count, baseline.accepted_count)
        everything = scanner.scan("TST_LIQUID", ScanMode.RESEARCH, top_n=1,
                                  deprioritized=("XTST:SYM0", "XTST:SYM1"))
        self.assertEqual(everything.candidates[0].instrument_id, first)


class _RotatingResearch:
    """Returns the first non-deprioritized instrument, mirroring the scanner's ordering."""

    universe = (AAPL, MSFT)

    def __init__(self):
        self.calls = []

    def run(self, universe_id, *, idempotency_key, mode, as_of, top_n, validated_selection,
            deprioritized=()):
        assert validated_selection is True
        self.calls.append(deprioritized)
        ordered = sorted(self.universe, key=lambda item: (item in deprioritized, item))
        return {"run_id": f"research-{len(self.calls)}", "status": "COMPLETE",
                "candidates": [{"instrument_id": ordered[0], "stage": "STRATEGY_SELECTED"}]}


class _Proposals:
    """HOLD for AAPL; MSFT reaches risk but is rejected there."""

    def evaluate(self, run_id, candidate_id, *, evaluation_as_of):
        if candidate_id == AAPL:
            return {"status": "NON_ACTIONABLE", "reason": "NO_SIGNAL"}
        return {"status": "RISK_REJECTED", "reason": "MAX_POSITION_RISK"}


class CycleIntegrationTests(TestCase):
    def setUp(self):
        self.database = SQLiteDatabase()
        migrate(self.database)
        self.store = Store(self.database)
        self.addCleanup(self.database.close)
        self.clock = {"now": NOW}
        self.research = _RotatingResearch()
        self.execution = _Execution()

    def service(self, positions=None, discovery=SETTINGS):
        return AutonomousPaperTradingService(
            repository=self.store.autonomous_paper_cycles, research=self.research,
            signals=_Signals(), proposals=_Proposals(), execution=self.execution,
            positions=positions or _Positions(), gate=_Gate(), max_candidates=3,
            clock=lambda: self.clock["now"], heartbeat_interval=60, discovery=discovery)

    def cycle(self, service, minutes):
        self.clock["now"] = NOW + timedelta(minutes=minutes)
        return service.run("us-equities", idempotency_key=f"cycle-{minutes}", operator="operator",
                           as_of=self.clock["now"], top_n=1)

    def test_long_hold_rotates_to_new_candidate_which_still_needs_risk_approval(self):
        service = self.service()
        for minutes in (0, 15):
            self.cycle(service, minutes)
        self.assertEqual(self.research.calls, [(), ()])
        result = self.cycle(service, 30)
        self.assertEqual(self.research.calls[-1], (AAPL,))
        discovery = result["payload"]["discovery"]
        self.assertEqual(discovery["deprioritized"], [AAPL])
        reasons = {(item["instrument_id"], item["reason_code"]) for item in discovery["decisions"]}
        self.assertIn((AAPL, "HOLD_THRESHOLD_EXCEEDED"), reasons)
        self.assertIn((MSFT, "NEW_SCANNER_CANDIDATE"), reasons)
        self.assertEqual(result["candidates"][0]["status"], "RISK_REJECTED")
        self.assertEqual(self.execution.calls, [])
        self.assertEqual(self.cycle(service, 30)["run_id"], result["run_id"])
        self.assertEqual(len(self.research.calls), 3)

    def test_disabled_discovery_keeps_existing_research_request(self):
        service = self.service(discovery=None)
        for minutes in (0, 15, 30, 45):
            result = self.cycle(service, minutes)
        self.assertEqual(self.research.calls, [()] * 4)
        self.assertNotIn("discovery", result["payload"])

    def test_open_position_is_never_rotated(self):
        class Held:
            def manage(self):
                return {"positions": [{"instrument_id": AAPL, "status": "MONITORED"}], "orders": []}

        service = self.service(positions=Held())
        for minutes in (0, 15, 30, 45):
            result = self.cycle(service, minutes)
        self.assertEqual(result["payload"]["discovery"]["deprioritized"], [])
        self.assertEqual(self.research.calls[-1], ())

    def test_recovery_reuses_the_persisted_plan(self):
        service = self.service()
        key = "interrupted-key"
        persisted_plan = {"deprioritized": [MSFT], "decisions": [], "at": NOW.isoformat()}
        self.store.autonomous_paper_cycles.create(
            run_id="interrupted", idempotency_key=key,
            request_hash=service._request_hash("us-equities", NOW, 1, "operator"),
            universe_id="us-equities", market_scope="PAPER", account_id="PAPER", as_of=NOW,
            payload={"request": {"universe_id": "us-equities", "idempotency_key": key,
                                 "operator": "operator", "mode": "PAPER",
                                 "as_of": NOW.isoformat(), "top_n": 1},
                     "discovery": persisted_plan},
            owner_token="previous-process", lease_until=NOW - timedelta(seconds=1))
        recovered = service.recover("interrupted")
        self.assertEqual(self.research.calls, [(MSFT,)])
        self.assertEqual(recovered["payload"]["discovery"], persisted_plan)

    def test_concurrent_cycle_cannot_plan_or_research_in_parallel(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking:
            def manage(self):
                entered.set()
                release.wait(timeout=5)
                return {"positions": [], "orders": []}

        first = self.service(positions=Blocking())
        thread = threading.Thread(target=lambda: self.cycle(first, 0))
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            with self.assertRaises(AutonomousPaperCycleError):
                self.service().run("us-equities", idempotency_key="parallel", operator="operator",
                                   as_of=NOW, top_n=1)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertEqual(len(self.research.calls), 1)


class EvidenceExpirySettingTests(TestCase):
    def test_expiry_is_operator_defined_with_no_default(self):
        from stockmarket.api.bootstrap import _evidence_max_age_from_env
        self.assertIsNone(_evidence_max_age_from_env({}))
        self.assertEqual(_evidence_max_age_from_env({"AUTOMATIC_EVIDENCE_MAX_AGE_DAYS": "30"}),
                         timedelta(days=30))
        for bad in ("0", "366", "thirty"):
            with self.assertRaises(ConfigurationError):
                _evidence_max_age_from_env({"AUTOMATIC_EVIDENCE_MAX_AGE_DAYS": bad})
