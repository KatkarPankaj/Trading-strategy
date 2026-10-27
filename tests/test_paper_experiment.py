from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import test_paper_end_to_end as fixtures
from stockmarket.core.executors import TradingMode
from stockmarket.core.paper_experiment import PaperExperimentService
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.persistence.repositories import ProspectiveRepository


class PaperExperimentTests(TestCase):
    def setUp(self):
        self.db = SQLiteDatabase()
        migrate(self.db)
        self.addCleanup(self.db.close)
        self.store = Store(self.db)
        self.at = datetime(2026, 10, 26, 18, tzinfo=timezone.utc)
        self.service = PaperExperimentService(
            self.store, mode=TradingMode.PAPER, clock=lambda: self.at)
        self.request = {
            "experiment_id": "us-30d", "start_date": date(2026, 10, 27),
            "duration_days": 30, "operator": "fixture",
            "acceptance_policy_ref": "reviewed-protocol-reference",
            "runtime": {"mode": "PAPER", "single_market": True,
                        "timezone": "America/New_York"},
        }

    def test_calendar_days_include_weekends_and_dst_without_claiming_completion(self):
        record = self.service.register(**self.request)
        protocol = record["protocol"]
        self.assertEqual(record["phase"], "SCHEDULED")
        self.assertEqual(protocol["starts_at"], "2026-10-27T04:00:00+00:00")
        self.assertEqual(protocol["ends_at_exclusive"], "2026-11-26T05:00:00+00:00")
        self.at = datetime.fromisoformat(protocol["starts_at"])
        self.assertEqual(self.service.status("us-30d")["phase"], "OBSERVATION_WINDOW")
        self.at = datetime.fromisoformat(protocol["ends_at_exclusive"])
        result = self.service.status("us-30d")
        self.assertEqual(result["phase"], "EVALUATION_DUE")
        self.assertEqual(result["evaluation"], "NOT_PERFORMED")
        self.assertEqual(result["execution"], "NOT_AUTHORIZED_BY_REGISTRATION")

    def test_identical_registration_replays_but_changed_protocol_is_rejected(self):
        first = self.service.register(**self.request)
        self.at += timedelta(days=2)
        replay = self.service.register(**self.request)
        self.assertEqual(replay["protocol_hash"], first["protocol_hash"])
        self.assertEqual(replay["created_at"], first["created_at"])
        for changes in ({"duration_days": 31}, {"runtime": {
                **self.request["runtime"], "configuration_hash": "changed"}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "different protocol"):
                self.service.register(**{**self.request, **changes})

    def test_refuses_backfill_invalid_duration_and_missing_policy(self):
        for changes in (
            {"start_date": date(2026, 10, 26)}, {"duration_days": True},
            {"duration_days": 0}, {"duration_days": 367}, {"operator": " "},
            {"acceptance_policy_ref": " "},
            {"runtime": {**self.request["runtime"], "mode": "LIVE"}},
            {"runtime": {**self.request["runtime"], "single_market": False}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.register(**{**self.request, **changes})
        with self.assertRaises(ValueError):
            PaperExperimentService(self.store, mode=TradingMode.LIVE, clock=lambda: self.at)

    def test_integrity_failure_is_not_a_successful_status(self):
        self.service.register(**self.request)
        self.db.execute(
            "UPDATE prospective_experiments SET payload = ? WHERE experiment_id = ?",
            ('{}', "us-30d"))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.service.status("us-30d")

    def test_disk_restart_preserves_protocol_and_status_is_read_only(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiment.db"
            db = SQLiteDatabase(str(path))
            try:
                migrate(db)
                service = PaperExperimentService(
                    Store(db), mode=TradingMode.PAPER, clock=lambda: self.at)
                expected = service.register(**self.request)
            finally:
                db.close()
            db = SQLiteDatabase(str(path))
            try:
                service = PaperExperimentService(
                    Store(db), mode=TradingMode.PAPER, clock=lambda: self.at)
                self.assertEqual(service.status("us-30d"), expected)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM fills")[0]["n"], 0)
            finally:
                db.close()


class PaperExperimentApiTests(TestCase):
    def setUp(self):
        self.f = fixtures.PaperEndToEndTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = self.f.context(auto_loop=True)
        self.client = self.f.client(self.ctx)
        self.body = {
            "experiment_id": "us-api-30d",
            "start_date": (self.f.clock.at.date() + timedelta(days=1)).isoformat(),
            "benchmark_instrument_id": self.f.instrument_id, "operator": "fixture",
            "acceptance_policy_ref": "operator-policy-ref",
        }

    def test_actual_api_freezes_runtime_and_never_starts_loop_or_creates_orders(self):
        before = self.f.state(self.client)
        result = self.client.post("/paper/experiments", headers=fixtures.HEADERS, json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        record = result.json()
        runtime = record["protocol"]["runtime"]
        self.assertEqual(record["protocol"]["duration_calendar_days"], 30)
        self.assertEqual(runtime["loop"]["universe_id"], self.ctx.paper_loop.settings.universe_id)
        self.assertEqual(runtime["risk"]["max_drawdown_pct"], self.ctx.settings.risk.max_drawdown_pct)
        baseline = runtime["strategy_baselines"]["orb_vwap"]
        self.assertEqual(baseline["version"], "1.0.0")
        self.assertIsNotNone(baseline["parameters_hash"])
        self.assertEqual(record["protocol_hash"], ProspectiveRepository.digest(record["protocol"]))
        self.assertEqual(self.f.state(self.client), before)
        self.assertEqual(self.f.store.autonomous_paper_cycles.recent(1), [])
        shown = self.client.get("/paper/experiments/us-api-30d", headers=fixtures.HEADERS)
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(shown.json()["protocol"], record["protocol"])

    def test_authentication_unknown_benchmark_and_invalid_request_are_explicit(self):
        self.assertEqual(self.client.post("/paper/experiments", json=self.body).status_code, 401)
        result = self.client.post("/paper/experiments", headers=fixtures.HEADERS,
                                  json={**self.body, "benchmark_instrument_id": "XNSE:UNKNOWN"})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.client.post("/paper/experiments", headers=fixtures.HEADERS,
                                         json={**self.body, "duration_days": True}).status_code, 422)
        self.assertEqual(self.client.get("/paper/experiments/unknown",
                                        headers=fixtures.HEADERS).status_code, 404)

    def test_registration_cannot_clear_halt_or_enable_trading(self):
        self.ctx.gate.halt("EXPERIMENT_TEST", "risk gate must remain authoritative")
        self.ctx.paper_loop.set_entries_enabled(
            False, operator="fixture", reason="test preserved pause")
        result = self.client.post("/paper/experiments", headers=fixtures.HEADERS, json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(self.ctx.gate.halted)
        self.assertFalse(self.ctx.paper_loop.status()["operator_entries_enabled"])
        self.assertEqual(self.f.store.orders.all(), [])
        self.assertEqual(self.f.store.fills.all(), [])

    def test_api_rejects_live_mode_and_missing_automatic_runtime(self):
        self.ctx.live = self.ctx.paper
        self.assertEqual(self.client.post("/paper/experiments", headers=fixtures.HEADERS,
                                         json=self.body).status_code, 403)
        self.ctx.live = None
        self.ctx.paper_loop = None
        self.assertEqual(self.client.post("/paper/experiments", headers=fixtures.HEADERS,
                                         json=self.body).status_code, 409)
