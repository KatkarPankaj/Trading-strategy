from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_paper_end_to_end as paper_fixture
from test_paper_end_to_end import HEADERS
from stockmarket.core.persistence.repositories import ProspectiveRepository
from stockmarket.core.models import OrderStatus
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.strategies import OrbVwapStrategy
from stockmarket.core.strategies.orb_vwap import OrbVwapConfig


class ProspectiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = paper_fixture.PaperEndToEndTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ctx = self.fixture.context()
        self.client = self.fixture.client(self.ctx)
        self.service = self.ctx.prospective
        self.store = self.fixture.store

    def decision(self):
        self.fixture.run_research(self.client)
        rows = self.store.prospective.decisions()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_atomic_capture_immutability_no_backfill_and_gets_are_read_only(self):
        decision = self.decision()
        self.assertEqual(decision["selected_strategy"], "orb_vwap")
        self.assertEqual(decision["timeframe"], "5m")
        self.assertEqual(decision["feature_hash"], ProspectiveRepository.digest(decision["features"]))
        self.assertEqual(decision["risk_status"], "NOT_AUTHORIZED_BY_RESEARCH")
        self.assertIn("limits", decision["risk_assumptions"])
        self.assertEqual(decision["strategy_catalog"]["orb_vwap"]["version"], "1.0.0")
        self.store.prospective.save_decision(decision)
        changed = deepcopy(decision)
        changed["selected_strategy"] = "NO_TRADE"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.prospective.save_decision(changed)
        before = self.fixture.state(self.client)
        with patch.object(self.service, "reconcile", side_effect=AssertionError("GET mutation")):
            for path in ("/intelligence/prospective/decisions",
                         f"/intelligence/prospective/decisions/{decision['decision_id']}",
                         "/intelligence/prospective/readiness"):
                self.assertEqual(self.client.get(path, headers=HEADERS).status_code, 200)
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.fixture.state(self.client), before)
        report = self.service.readiness()
        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["training"], "NOT_PERFORMED")
        self.assertEqual(report["fixture_capture_count"], 1)
        self.assertIn("sector", report["missing_feature_counts"])
        self.assertEqual(self.ctx.prospective.reconcile()["attachments_added"], 0)
        restarted = self.fixture.context()
        self.assertEqual(restarted.store.prospective.decisions(), [decision])

    def test_completed_actual_paper_trade_and_recovery_attach_once(self):
        proposal = self.fixture.propose(self.client)
        decision = self.store.prospective.decisions()[0]
        self.assertEqual(self.fixture.submit(self.client, proposal["proposal_id"]).status_code, 200)
        first = self.service.reconcile()
        self.assertGreater(first["attachments_added"], 0)
        self.assertEqual(self.service.observation(decision["decision_id"])["status"],
                         "FILLED_AWAITING_CLOSED_OUTCOME")
        self.assertEqual(self.service.reconcile()["attachments_added"], 0)
        self.fixture.clock.at += timedelta(minutes=1)
        self.fixture.data.price = 100
        self.ctx.paper_position_manager.manage()
        self.assertGreater(self.service.reconcile()["attachments_added"], 0)
        result = self.service.observation(decision["decision_id"])
        self.assertEqual(result["status"], "LABELED_CLOSED_EPISODE")
        self.assertEqual(len(result["labels"]), 1)
        self.assertLess(result["labels"][0]["net_return"], 0)
        self.assertFalse(result["labels"][0]["profitable"])
        self.assertTrue({"ORDER", "FILL", "ORDER_EVENT", "RISK", "SIGNAL", "OUTCOME"}.issubset(
            {a["kind"] for a in result["attachments"]}))
        self.assertEqual(self.store.prospective.decision(decision["decision_id"]), decision)
        restarted = self.fixture.context()
        self.assertEqual(restarted.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(restarted.prospective.observation(decision["decision_id"]), result)
        self.assertEqual(len(self.store.paper_outcomes.recent()), 1)

    def test_recording_failure_rolls_back_assessment_and_no_order_is_created(self):
        with patch.object(ProspectiveRepository, "save_decision", side_effect=RuntimeError("ledger disk failure")):
            response = self.client.post("/research/autonomous", headers=HEADERS, json={
                "universe_id": "US_ALL", "idempotency_key": "capture-error",
                "as_of": self.fixture.clock.at.isoformat(), "top_n": 1})
        self.assertIn(response.status_code, (200, 503))
        self.assertEqual(self.store.prospective.decisions(), [])
        self.assertEqual(self.store.db.query("SELECT COUNT(*) AS n FROM ai_research_assessments")[0]["n"], 0)
        self.assertEqual(self.store.orders.all(), [])

    def test_unfilled_partial_entry_partial_exit_and_missing_outcome(self):
        proposal = self.fixture.propose(self.client)
        decision = self.store.prospective.decisions()[0]
        broker = self.ctx.recovery._broker
        manager = self.ctx.paper.order_manager
        with patch.object(broker, "_apply_match"):
            self.assertEqual(self.fixture.submit(self.client, proposal["proposal_id"]).status_code, 200)
        self.service.reconcile()
        self.assertEqual(self.service.observation(decision["decision_id"])["status"],
                         "UNFILLED_OR_REJECTED_ORDER")
        original_match = broker._apply_match

        def partial_match(record, *, persist):
            # Exercise normal broker accounting with bounded simulated liquidity, not market evidence.
            quantity = record.quantity
            record.quantity = min(quantity, record.filled + 40)
            try:
                original_match(record, persist=False)
            finally:
                record.quantity = quantity
            if record.status is OrderStatus.FILLED and record.filled < quantity:
                record.status = OrderStatus.PARTIALLY_FILLED
            if persist:
                broker._save_rec(record)

        entry_id = self.store.orders.all()[0]["client_order_id"]
        with patch.object(broker, "_apply_match", side_effect=partial_match):
            broker.update_price(self.fixture.instrument_id, 102, self.fixture.clock.at)
        manager.sync(entry_id)
        self.service.reconcile()
        self.assertEqual(self.store.fills.for_order(entry_id)[0]["quantity"], 40)
        self.assertEqual(self.service.observation(decision["decision_id"])["labels"], [])
        self.fixture.clock.at += timedelta(minutes=1)
        broker.update_price(self.fixture.instrument_id, 102, self.fixture.clock.at)
        manager.sync(entry_id)
        self.service.reconcile()
        self.fixture.clock.at += timedelta(minutes=1)
        self.fixture.data.price = 100
        with patch.object(broker, "_apply_match", side_effect=partial_match):
            self.ctx.paper_position_manager.manage()
        self.service.reconcile()
        observation = self.service.observation(decision["decision_id"])
        self.assertEqual(observation["status"], "FILLED_AWAITING_CLOSED_OUTCOME")
        self.assertEqual(observation["labels"], [])
        exits = [a for a in observation["attachments"] if a["kind"] == "ORDER" and not a["payload"]["entry"]]
        self.assertEqual(len(exits), 1)
        exit_id = exits[0]["source_id"]
        self.assertEqual(self.store.fills.for_order(exit_id)[0]["quantity"], 40)
        self.fixture.clock.at += timedelta(minutes=1)
        broker.update_price(self.fixture.instrument_id, 100, self.fixture.clock.at)
        manager.sync(exit_id)
        self.ctx.paper_position_manager.manage()
        self.service.reconcile()
        labels = self.service.observation(decision["decision_id"])["labels"]
        self.assertEqual(len(labels), 1)
        self.assertAlmostEqual(labels[0]["net_return"], -200 / 10200)
        self.assertEqual(self.service.reconcile()["attachments_added"], 0)

    def test_execution_version_mismatch_rolls_back_reconciliation(self):
        proposal = self.fixture.propose(self.client)
        self.fixture.submit(self.client, proposal["proposal_id"])
        decision = self.store.prospective.decisions()[0]
        self.store.db.execute("UPDATE execution_records SET strategy_version = 'altered'")
        with self.assertRaisesRegex(ValueError, "version differs"):
            self.service.reconcile()
        self.assertEqual(self.store.prospective.attachments(decision["decision_id"]), [])

    def test_unavailable_ai_is_an_immutable_no_trade_observation(self):
        with patch.object(self.fixture.ai, "complete", side_effect=RuntimeError("fixture AI unavailable")):
            decision = self.decision()
        self.assertEqual(decision["selected_strategy"], "NO_TRADE")
        self.assertEqual(decision["status"], "AI_UNAVAILABLE")
        self.service.reconcile()
        observation = self.service.observation(decision["decision_id"])
        self.assertEqual(observation["status"], "NO_TRADE")
        self.assertEqual(observation["labels"], [])
        self.assertEqual(self.store.orders.all(), [])

    def test_disk_reopen_preserves_decision_fills_outcomes_and_links(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "prospective.db")
            db = SQLiteDatabase(path)
            try:
                migrate(db)
                store = Store(db)
                store.instruments.save(self.ctx.instruments[self.fixture.instrument_id])
                self.fixture.store = store
                context = self.fixture.context()
                client = self.fixture.client(context)
                proposal = self.fixture.propose(client)
                self.fixture.submit(client, proposal["proposal_id"])
                self.fixture.clock.at += timedelta(minutes=1)
                self.fixture.data.price = 100
                context.paper_position_manager.manage()
                context.prospective.reconcile()
                decision = store.prospective.decisions()[0]
                observation = context.prospective.observation(decision["decision_id"])
            finally:
                db.close()
            reopened_db = SQLiteDatabase(path)
            try:
                migrate(reopened_db)
                self.fixture.store = Store(reopened_db)
                restarted = self.fixture.context()
                self.assertEqual(restarted.prospective.reconcile()["attachments_added"], 0)
                self.assertEqual(restarted.prospective.observation(decision["decision_id"]), observation)
                self.assertEqual(len(restarted.store.fills.all()), 2)
                self.assertEqual(len(restarted.store.paper_outcomes.recent()), 1)
                self.assertEqual(restarted.paper.portfolio.cash["USD"], 99_800)
            finally:
                reopened_db.close()

    def test_future_features_and_catalog_changes_cannot_be_frozen(self):
        decision = self.decision()
        opportunity = self.ctx.candidate_assessment.assess(decision["research_context"]["snapshot_id"])
        context = opportunity.assessment.input_context
        future = replace(context, snapshot_created_at=self.fixture.clock.at + timedelta(seconds=1))
        altered = replace(opportunity, assessment=replace(opportunity.assessment, input_context=future))
        with self.assertRaisesRegex(ValueError, "precedes"):
            self.service.snapshot(altered)
        evidence = [dict(e) for e in context.evidence]
        evidence[0]["retrieved_at"] = (self.fixture.clock.at + timedelta(seconds=1)).isoformat()
        altered = replace(opportunity, assessment=replace(
            opportunity.assessment, input_context=replace(context, evidence=tuple(evidence))))
        with self.assertRaisesRegex(ValueError, "future/unavailable"):
            self.service.snapshot(altered)
        changed = {"orb_vwap": OrbVwapStrategy(OrbVwapConfig(volume_spike_threshold=2))}
        with patch.object(self.service, "strategies", return_value=changed):
            with self.assertRaisesRegex(ValueError, "catalog changed"):
                self.service.snapshot(opportunity)
        added = {**self.service.strategies(), "added_during_assessment": OrbVwapStrategy()}
        with patch.object(self.service, "strategies", return_value=added):
            with self.assertRaisesRegex(ValueError, "catalog changed"):
                self.service.snapshot(opportunity)

    def test_integrity_tampering_and_changed_fill_are_visible_failures(self):
        proposal = self.fixture.propose(self.client)
        self.fixture.submit(self.client, proposal["proposal_id"])
        self.service.reconcile()
        self.store.db.execute("UPDATE fills SET fee = fee + 1")
        with self.assertRaisesRegex(ValueError, "source record changed"):
            self.service.reconcile()
        self.store.db.execute("UPDATE prospective_decisions SET payload = ?", (json.dumps({"altered": True}),))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.prospective.decisions()
        self.assertEqual(self.client.get("/intelligence/prospective/decisions", headers=HEADERS).status_code, 409)

    def test_unsubmitted_and_risk_rejected_observations_are_censored_not_zero_labels(self):
        proposal = self.fixture.propose(self.client)
        decision = self.store.prospective.decisions()[0]
        self.service.reconcile()
        observation = self.service.observation(decision["decision_id"])
        self.assertEqual(observation["status"], "NO_EXECUTION_OBSERVED")
        self.assertEqual(observation["labels"], [])
        self.ctx.kill_switch.trigger("fixture", "keep risk authoritative", source="manual", actor="fixture")
        rejected = self.fixture.submit(self.client, proposal["proposal_id"])
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.service.reconcile()
        observation = self.service.observation(decision["decision_id"])
        self.assertEqual(observation["status"], "UNFILLED_OR_REJECTED_ORDER")
        self.assertTrue(observation["censored"])
        self.assertEqual(observation["labels"], [])
        self.assertEqual(self.store.fills.all(), [])


class ProspectiveDatasetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = paper_fixture.PaperEndToEndTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ctx = self.fixture.context()
        self.client = self.fixture.client(self.ctx)
        self.fixture.run_research(self.client)
        self.service, self.store = self.ctx.prospective, self.fixture.store
        self.template = self.store.prospective.decisions()[0]
        self.base = self.fixture.clock.at
        self.fixture.clock.at += timedelta(days=50)
        self.policy = {"as_of": self.base + timedelta(days=45), "train_end": self.base + timedelta(days=10),
            "validation_end": self.base + timedelta(days=20), "test_end": self.base + timedelta(days=30),
            "final_end": self.base + timedelta(days=40), "embargo_hours": 24}

    def row(self, identity, day, known_day, *, provider="unattested-boundary-source", schema=None):
        # Synthetic mechanics-only fixtures: readiness must never certify them as real market evidence.
        row = deepcopy(self.template)
        at = self.base + timedelta(days=day)
        row.update({"decision_id": identity, "opportunity_id": identity, "decision_at": at.isoformat(),
                    "recorded_at": at.isoformat(), "research_as_of": at.isoformat(),
                    "market_data_provider": provider})
        for feature in row["features"].values():
            feature.update({"value": .1, "available_at": at.isoformat()})
        row["feature_hash"] = ProspectiveRepository.digest(row["features"])
        if schema:
            row["feature_schema"] = schema
        self.store.prospective.save_decision(row)
        label = {"net_return": .01, "profitable": True,
                 "known_at": (self.base + timedelta(days=known_day)).isoformat(),
                 "opened_at": at.isoformat(), "closed_at": (at + timedelta(minutes=5)).isoformat(),
                 "outcome_id": identity}
        self.store.prospective.save_attachment(
            {"decision_id": identity, "kind": "OUTCOME", "source_id": identity,
             "payload": {"label_status": "ELIGIBLE", "label": label, "label_schema": row["label_schema"]}},
            self.fixture.clock.at)
        return row

    def test_chronological_labels_embargo_versions_final_membership_hashes_and_replay(self):
        self.row("train", 2, 3)
        self.row("late-label", 8, 12)
        self.row("embargo", 10.5, 11)
        self.row("validate", 12, 13)
        self.row("test", 22, 23)
        self.row("final", 32, 33)
        self.row("schema-change", 4, 5, schema="features-v2")
        self.row("fixture", 4, 5, provider="synthetic-fixture")
        manifest = self.service.create_dataset(**self.policy)
        self.assertEqual({name: [r["decision_id"] for r in rows] for name, rows in manifest["partitions"].items()},
            {"TRAIN": ["train"], "VALIDATION": ["validate"], "TEST": ["test"], "FINAL_HOLDOUT": ["final"]})
        excluded = {r["decision_id"]: r["reason"] for r in manifest["exclusions"]}
        self.assertIn("late-label", excluded)
        self.assertIn("embargo", excluded)
        self.assertEqual(excluded["schema-change"], "SCHEMA_VERSION_MISMATCH")
        self.assertEqual(excluded["fixture"], "FIXTURE_OR_SYNTHETIC_CAPTURE")
        self.assertEqual(manifest["dataset_hash"], ProspectiveRepository.digest(
            {"policy": manifest["policy"], "partitions": manifest["partitions"]}))
        self.assertEqual(manifest["readiness"]["status"], "NOT_READY")
        self.assertEqual(manifest["readiness"]["training"], "NOT_PERFORMED")
        self.assertEqual(self.service.create_dataset(**self.policy), manifest)
        restarted = self.fixture.context()
        self.assertEqual(restarted.prospective.create_dataset(**self.policy), manifest)
        another = self.service.create_dataset(**{**self.policy, "as_of": self.policy["as_of"] + timedelta(days=1)})
        self.assertEqual(sum(len(rows) for rows in another["partitions"].values()), 0)
        self.assertIn("final", {r["decision_id"] for r in another["exclusions"]
                              if r["reason"] == "ALREADY_ASSIGNED_TO_FROZEN_DATASET"})
        for path in (f"/intelligence/prospective/datasets/{manifest['manifest_id']}",):
            with patch.object(self.service, "reconcile", side_effect=AssertionError("GET write")):
                response = self.client.get(path, headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["monitoring_only"])
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_reserved_holdout_and_unavailable_features_cannot_enter_training(self):
        row = self.row("reserved", 2, 3)
        self.row("future-feature", 4, 5)
        proposal = {"proposal_id": "fixture-reservation", "instrument_id": row["instrument_id"]}
        self.store.strategy_lab.save_proposal(proposal, self.base)
        self.store.strategy_lab.save_holdout({
            "proposal_id": proposal["proposal_id"], "start": self.base.isoformat(),
            "end": (self.base + timedelta(days=3)).isoformat()}, self.base)
        original = self.store.prospective.decision("future-feature")
        original["features"]["volume"]["available_at"] = (self.base + timedelta(days=5)).isoformat()
        original["feature_hash"] = ProspectiveRepository.digest(original["features"])
        # Simulate semantic corruption with a recomputed payload checksum, not valid collection.
        self.store.db.execute("UPDATE prospective_decisions SET payload = ?, payload_hash = ? WHERE decision_id = ?",
            (json.dumps(original, sort_keys=True, separators=(",", ":")), ProspectiveRepository.digest(original),
             "future-feature"))
        manifest = self.service.create_dataset(**self.policy)
        self.assertEqual(manifest["partitions"]["TRAIN"], [])
        excluded = {r["decision_id"]: r["reason"] for r in manifest["exclusions"]}
        self.assertEqual(excluded["reserved"], "EMBARGO_LABEL_BOUNDARY_OR_RESERVED_HOLDOUT")
        self.assertEqual(excluded["future-feature"], "FEATURE_NOT_AVAILABLE_AT_DECISION")

    def test_dataset_api_requires_auth_valid_dates_and_preserves_empty_manifest(self):
        payload = {key: value.isoformat() if isinstance(value, datetime) else value
                   for key, value in self.policy.items()}
        self.assertEqual(self.client.post("/intelligence/prospective/datasets", json=payload).status_code, 401)
        self.assertEqual(self.client.post("/intelligence/prospective/reconcile").status_code, 401)
        response = self.client.post("/intelligence/prospective/datasets", headers=HEADERS, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sum(len(rows) for rows in response.json()["partitions"].values()), 0)
        self.assertEqual(response.json()["readiness"]["status"], "NOT_READY")
        self.assertEqual(self.client.post("/intelligence/prospective/datasets", headers=HEADERS,
            json={**payload, "train_end": "2026-10-01"}).status_code, 422)
        self.assertEqual(self.client.post("/intelligence/prospective/datasets", headers=HEADERS,
            json={**payload, "train_end": payload["test_end"]}).status_code, 422)

    def test_invalid_feature_hash_and_nonfinite_labels_fail_visibly(self):
        row = self.row("bad-feature-hash", 2, 3)
        row["feature_hash"] = "altered"
        self.store.db.execute(
            "UPDATE prospective_decisions SET payload = ?, payload_hash = ? WHERE decision_id = ?",
            (json.dumps(row, sort_keys=True, separators=(",", ":")), ProspectiveRepository.digest(row),
             row["decision_id"]))
        manifest = self.service.create_dataset(**self.policy)
        excluded = {r["decision_id"]: r["reason"] for r in manifest["exclusions"]}
        self.assertEqual(excluded["bad-feature-hash"], "FEATURE_HASH_MISMATCH")
        self.row("bad-label", 4, 5)
        attachment = self.store.prospective.attachments("bad-label")[0]
        attachment.pop("recorded_at")
        attachment["payload"]["label"]["net_return"] = float("nan")
        self.store.db.execute(
            "UPDATE prospective_attachments SET payload = ?, payload_hash = ? WHERE decision_id = ?",
            (json.dumps(attachment, sort_keys=True, separators=(",", ":")),
             ProspectiveRepository.digest(attachment), "bad-label"))
        with self.assertRaisesRegex(ValueError, "invalid prospective outcome label"):
            self.service.observation("bad-label")
