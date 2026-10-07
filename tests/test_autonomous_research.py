from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stockmarket.core.autonomous_research import (
    AutonomousResearchError,
    AutonomousResearchService,
    AutonomousResearchSettings,
    IdempotencyConflict,
)
from stockmarket.core.models import AssetClass

AS_OF = datetime(2025, 1, 7, 15, 0, tzinfo=timezone.utc)


class _Calendar:
    def is_trading_day(self, day):
        return day.weekday() < 5


class _Market:
    timezone = "UTC"
    calendar = _Calendar()

    def is_covered(self, day):
        return True


class _Markets:
    def get(self, code):
        return _Market()


class _ScanRepository:
    def __init__(self):
        self.rows = {}

    def get(self, scan_id):
        return self.rows.get(scan_id)

    def candidates(self, scan_id, **kwargs):
        return [{"instrument_id": "US:ABC"}]


class _Scanner:
    def __init__(self):
        self.repository = _ScanRepository()
        self.calls = 0
        self.modes = []
        self.max_concurrency = 4
        self.configuration_fingerprint = "scanner-config"

    def get_universe(self, universe_id):
        return type("Definition", (), {
            "markets": ("US",),
            "asset_classes": (AssetClass.EQUITY,),
        })(), ()

    def scan(self, universe_id, mode, **kwargs):
        self.calls += 1
        self.modes.append(mode)
        scan_id = kwargs["scan_id"]
        self.repository.rows[scan_id] = {"status": "COMPLETE"}
        return type("Result", (), {"status": type("Status", (), {"value": "COMPLETE"})()})()


class _ResearchRepository:
    def __init__(self):
        self.run = None
        self.rows = {}

    def get_run(self, run_id):
        return self.run if self.run and self.run["run_id"] == run_id else None

    def snapshots(self, run_id):
        return [{"snapshot_id": key, "instrument_id": row["instrument_id"]}
                for key, row in self.rows.items()]

    def get_snapshot(self, snapshot_id):
        return self.rows.get(snapshot_id)


class _Research:
    def __init__(self):
        self.research_runs = _ResearchRepository()
        self.settings = type("Settings", (), {"max_concurrency": 4})()
        self.calls = 0
        self.configuration_fingerprint = "research-config"

    def run_scan(self, scan_id, **kwargs):
        self.calls += 1
        run_id = kwargs["run_id"]
        self.research_runs.run = {
            "run_id": run_id,
            "payload": {"configuration_fingerprint": self.configuration_fingerprint},
        }
        snapshot_id = f"{run_id}-snapshot"
        self.research_runs.rows[snapshot_id] = {
            "instrument_id": "US:ABC",
            "payload": {"snapshot_id": snapshot_id},
        }
        return self.research_runs.run, ()


class _AssessmentRepository:
    def opportunity_for_snapshot(self, snapshot_id):
        return None


class _Assessment:
    strategies = {"orb_vwap": object()}
    repository = _AssessmentRepository()
    model_version = "unspecified"
    analyst = type("Analyst", (), {"provider_name": "fake"})()
    configuration_fingerprint = "assessment-config"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def assess(self, snapshot_id, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("test AI failure")
        return {
            "opportunity_id": "opportunity-1",
            "state": "STRATEGY_SELECTED",
            "assessment": {"recommended_strategy": "orb_vwap"},
        }


class _RunRepository:
    def __init__(self):
        self.runs = {}
        self.by_key = {}
        self.candidate_rows = {}

    def get_by_key(self, key):
        run_id = self.by_key.get(key)
        return self.runs.get(run_id) if run_id else None

    def create_run(self, *, run_id, idempotency_key, request_hash, as_of, now, payload):
        row = {
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "status": "RUNNING",
            "stage": "CREATED",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "as_of": as_of.isoformat(),
            "payload": payload,
        }
        self.runs[run_id] = row
        self.by_key[idempotency_key] = run_id

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def update_run(self, run_id, *, status, stage, now, payload):
        row = self.runs[run_id]
        row.update(status=status, stage=stage, updated_at=now.isoformat(), payload=payload)

    def save_candidate(self, run_id, instrument_id, *, stage, now, payload,
                       snapshot_id=None, opportunity_id=None, error=None):
        self.candidate_rows[(run_id, instrument_id)] = {
            "run_id": run_id,
            "instrument_id": instrument_id,
            "stage": stage,
            "snapshot_id": snapshot_id,
            "opportunity_id": opportunity_id,
            "error": error,
            "payload": payload,
        }

    def candidates(self, run_id):
        return [row for (saved_run, _), row in self.candidate_rows.items()
                if saved_run == run_id]


class AutonomousResearchTests(unittest.TestCase):
    def make_service(self, *, assessment=None, calendar=None):
        scanner = _Scanner()
        research = _Research()
        candidate_assessment = assessment or _Assessment()
        repository = _RunRepository()
        settings = AutonomousResearchSettings(
            allowed_markets=("US",),
            allowed_asset_classes=(AssetClass.EQUITY,),
            strategy_allowlist=("orb_vwap",),
        )
        service = AutonomousResearchService(
            scanner, research, candidate_assessment, repository, _Markets(), settings,
            clock=lambda: AS_OF,
        )
        return service, scanner, research, candidate_assessment, repository

    def test_research_run_stops_at_strategy_selection_and_reuses_idempotency_key(self):
        service, scanner, research, assessment, repository = self.make_service()

        result = service.run("universe", idempotency_key="daily-2025-01-07", as_of=AS_OF)
        repeated = service.run("universe", idempotency_key="daily-2025-01-07", as_of=AS_OF)

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["stage"], "STRATEGY_SELECTED")
        self.assertEqual(result["candidates"][0]["stage"], "STRATEGY_SELECTED")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        self.assertEqual(result["risk_status"], "NOT_EVALUATED")
        self.assertEqual(scanner.modes, ["RESEARCH"])
        self.assertNotIn("signal", result)
        self.assertNotIn("order", result)
        self.assertEqual(repeated["run_id"], result["run_id"])
        self.assertEqual((scanner.calls, research.calls, assessment.calls), (1, 1, 1))
        self.assertEqual(len(repository.candidates(result["run_id"])), 1)

    def test_live_mode_is_rejected(self):
        service, *_ = self.make_service()

        with self.assertRaisesRegex(AutonomousResearchError, "PAPER-only"):
            service.run("universe", idempotency_key="live-attempt", mode="LIVE")

    def test_idempotency_key_cannot_be_reused_for_different_request(self):
        service, *_ = self.make_service()
        service.run("universe", idempotency_key="same-key", as_of=AS_OF)

        with self.assertRaises(IdempotencyConflict):
            service.run("different-universe", idempotency_key="same-key", as_of=AS_OF)

    def test_assessment_failure_isolated_and_persisted_without_execution(self):
        service, _, _, _, repository = self.make_service(assessment=_Assessment(fail=True))

        result = service.run("universe", idempotency_key="isolated-failure", as_of=AS_OF)

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["candidates"][0]["stage"], "FAILED")
        self.assertEqual(result["candidates"][0]["execution"], "NOT_SUBMITTED")
        self.assertEqual(result["risk_status"], "NOT_EVALUATED")
        self.assertEqual(len(repository.candidates(result["run_id"])), 1)

    def test_interrupted_candidate_is_retried_without_repeating_scan_or_research(self):
        assessment = _Assessment(fail=True)
        service, scanner, research, _, repository = self.make_service(
            assessment=assessment)
        first = service.run(
            "universe", idempotency_key="resumable", as_of=AS_OF)
        assessment.fail = False

        resumed = service.run(
            "universe", idempotency_key="resumable", as_of=AS_OF)

        self.assertEqual(first["status"], "PARTIAL")
        self.assertEqual(resumed["status"], "COMPLETE")
        self.assertEqual(resumed["candidates"][0]["stage"], "STRATEGY_SELECTED")
        self.assertEqual((scanner.calls, research.calls, assessment.calls), (1, 1, 2))
        self.assertEqual(len(repository.candidates(resumed["run_id"])), 1)

    def test_non_trading_day_is_rejected(self):
        service, *_ = self.make_service()

        with self.assertRaisesRegex(AutonomousResearchError, "non-trading day"):
            service.run(
                "universe", idempotency_key="sunday",
                as_of=datetime(2025, 1, 5, 15, 0, tzinfo=timezone.utc),
            )

    def test_unregistered_strategy_allowlist_is_rejected_at_construction(self):
        scanner = _Scanner()
        research = _Research()
        settings = AutonomousResearchSettings(
            allowed_markets=("US",),
            allowed_asset_classes=(AssetClass.EQUITY,),
            strategy_allowlist=("not_registered",),
        )

        with self.assertRaisesRegex(ValueError, "unregistered strategies"):
            AutonomousResearchService(
                scanner, research, _Assessment(), _RunRepository(),
                _Markets(), settings)


if __name__ == "__main__":
    unittest.main()
