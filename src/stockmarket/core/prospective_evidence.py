"""Prospective-only decision capture, append-only follow-up and advisory dataset readiness."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Callable, Mapping
import json
import re

from .ai.candidate_assessment import AIResearchOpportunity
from .persistence import Store
from .persistence.repositories import ProspectiveRepository, to_json, ts
from .strategies.base import strategy_parameters, strategy_parameters_hash

FEATURE_SCHEMA = "decision-research-features-v1"
LABEL_SCHEMA = "selected-paper-episode-net-return-v1"
FEATURE_FIELDS = ("scanner_score", "evidence_completeness", "data_quality",
                  "volume", "momentum", "regime", "news", "fundamental", "sector")


def aware(value: str | datetime) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("prospective timestamps must be timezone-aware")
    return at


def plain(value: Any) -> Any:
    return json.loads(to_json(value))


def fixture_source(provider: str) -> bool:
    return re.search(r"(^|[-_ ])(fixture|synthetic|test)([-_ ]|$)", provider.lower()) is not None


class ProspectiveEvidenceService:
    def __init__(self, store: Store, *, clock: Callable[[], datetime],
                 strategies: Callable[[], Mapping[str, Any]], risk_assumptions: Mapping[str, Any],
                 provider: str) -> None:
        self.store, self.clock, self.strategies = store, clock, strategies
        self.risk_assumptions, self.provider = plain(risk_assumptions), provider

    def snapshot(self, opportunity: AIResearchOpportunity) -> Mapping[str, Any]:
        """Called before atomic assessment persistence; never reconstruct old decisions."""
        now = aware(self.clock())
        assessment, context = opportunity.assessment, opportunity.assessment.input_context
        if context.as_of > now or context.snapshot_created_at > now or assessment.assessed_at > now:
            raise ValueError("prospective decision precedes its evidence/assessment")
        catalog = {}
        for name, strategy in dict(self.strategies()).items():
            catalog[name] = {
                "version": getattr(strategy, "version", None),
                "parameters": strategy_parameters(strategy), "parameters_hash": strategy_parameters_hash(strategy),
                "timeframe": getattr(getattr(strategy, "config", None), "interval", None),
            }
        selected = assessment.recommended_strategy or "NO_TRADE"
        if set(catalog) != {registered["name"] for registered in context.registered_strategies}:
            raise ValueError("strategy catalog changed during prospective decision")
        for registered in context.registered_strategies:
            frozen = catalog.get(registered["name"])
            if frozen is None or frozen["version"] != registered["version"] \
                    or (frozen["parameters_hash"] or "UNVERSIONED") != registered["parameters_hash"]:
                raise ValueError("strategy catalog changed during prospective decision")
        if selected != "NO_TRADE" and selected not in catalog:
            raise ValueError("selected strategy is not in frozen decision catalog")
        features: dict[str, dict[str, Any]] = {}
        provenance = []
        for item in context.evidence:
            observed, retrieved = item.get("observed_at"), item.get("retrieved_at")
            if retrieved is None or aware(retrieved) > now \
                    or observed is not None and aware(observed) > context.as_of:
                raise ValueError("future/unavailable source evidence cannot enter a prospective decision")
            provenance.append(dict(item))
        available = context.snapshot_created_at
        for name in ("scanner_score", "evidence_completeness", "data_quality"):
            value = getattr(context, name)
            features[name] = self._feature(value, available, "persisted_research_snapshot")
        for name in FEATURE_FIELDS[3:]:
            source = [e for e in context.evidence if e.get("component") == name and e.get("quality") == "VALID"]
            value = context.technical_regime_scores.get(name)
            if source and value is not None:
                available_at = max([available] + [aware(e["retrieved_at"]) for e in source])
                features[name] = self._feature(value, available_at, "persisted_component_score")
            else:
                features[name] = {"value": None, "available_at": None, "missing_reason": "NO_VALID_TIMESTAMPED_SCORE"}
        ranking = assessment.strategy_ranking_evidence
        decision = {
            "decision_id": assessment.assessment_id, "opportunity_id": opportunity.opportunity_id,
            "decision_at": ts(now), "recorded_at": ts(now), "research_as_of": ts(context.as_of),
            "instrument_id": context.instrument_id, "market": context.market, "currency": context.currency,
            "timeframe": catalog[selected]["timeframe"] if selected != "NO_TRADE" else None,
            "feature_schema": FEATURE_SCHEMA, "label_schema": LABEL_SCHEMA,
            "features": features, "feature_hash": ProspectiveRepository.digest(features),
            "strategy_catalog": catalog, "selected_strategy": selected,
            "status": assessment.status.value, "reason": assessment.error or assessment.strategy_reason,
            "ranking_policy": ranking["policy"] if ranking else "AI_RESEARCH_ONLY",
            "scores": ranking, "risk_assumptions": self.risk_assumptions,
            "risk_status": "NOT_AUTHORIZED_BY_RESEARCH",
            "research_timeframes": sorted({
                str(e.get("payload", {}).get("interval")) for e in provenance
                if e.get("payload", {}).get("interval")}),
            "data_provenance": provenance, "market_data_provider": self.provider,
            "evidence_origin": "PROSPECTIVE_CAPTURE",
            "source_verification": "NOT_INDEPENDENTLY_ATTESTED",
            "research_context": context.as_mapping(),
            "assessment": plain(assessment),
            "limitations": ["Captured source responses do not certify vendor revision history.",
                            "Only future executed selected-strategy episodes can supply labels.",
                            "NO_TRADE and unexecuted alternatives have no counterfactual outcome label."],
        }
        return plain(decision)

    @staticmethod
    def _feature(value: Any, available: datetime, source: str) -> dict[str, Any]:
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
            raise ValueError("prospective feature must be a finite numeric observation")
        return {"value": value, "available_at": ts(available), "source": source}

    def reconcile(self) -> dict[str, Any]:
        """Explicit replay of durable sources; does not mutate outcomes, orders or decisions."""
        now = aware(self.clock())
        added = 0
        with self.store.db.transaction():
            decisions = self.store.prospective.decisions()
            fills = {f["fill_id"]: f for f in self.store.fills.all()}
            outcomes = self.store.paper_outcomes.available(now)
            for decision in decisions:
                iid, oid = decision["decision_id"], decision["opportunity_id"]
                at = aware(decision["decision_at"])
                # Older executions cannot be relabeled as prospective observations.
                generations = self.store.db.query(
                    "SELECT * FROM generated_strategy_signals WHERE opportunity_id = ?", (oid,))
                entry_orders = set()
                for generation in generations:
                    if not at <= aware(generation["generated_at"]) <= now:
                        raise ValueError("signal generation predates prospective decision or is in the future")
                    payload = {**generation, "payload": json.loads(generation["payload"])}
                    added += self._attach(iid, "SIGNAL", generation["generation_id"], payload, now)
                    risk_rows = self.store.db.query(
                        "SELECT * FROM risk_evaluations WHERE generation_id = ?", (generation["generation_id"],))
                    for risk in risk_rows:
                        if not at <= aware(risk["evaluated_at"]) <= now:
                            raise ValueError("risk evaluation outside prospective decision boundary")
                        added += self._attach(iid, "RISK", risk["evaluation_id"],
                                              {**risk, "payload": json.loads(risk["payload"])}, now)
                    if generation["signal_id"] is not None:
                        orders = self.store.db.query(
                            "SELECT client_order_id FROM execution_records WHERE signal_id = ? AND mode = 'PAPER'",
                            (generation["signal_id"],))
                        for row in orders:
                            entry_orders.add(row["client_order_id"])
                for order_id in sorted(entry_orders):
                    added += self._order_attachments(decision, order_id, now, entry=True)
                    for exit_proposal in self.store.position_exit_proposals.for_entry(order_id):
                        exit_id = exit_proposal["client_order_id"]
                        if self.store.orders.get(exit_id) is not None:
                            added += self._order_attachments(decision, exit_id, now, entry=False)
                for outcome in outcomes:
                    related = [fills[f] for f in outcome["fill_ids"] if f in fills
                               and fills[f]["client_order_id"] in entry_orders]
                    if not related:
                        continue
                    issue = self._outcome_issue(decision, outcome, fills, entry_orders)
                    if issue is None:
                        episode_orders = {fills[f]["client_order_id"] for f in outcome["fill_ids"]}
                        for order_id in sorted(episode_orders - entry_orders):
                            added += self._order_attachments(decision, order_id, now, entry=False)
                    added += self._attach(iid, "OUTCOME", outcome["outcome_id"],
                        {"outcome": outcome, "label_status": "ELIGIBLE" if issue is None else "EXCLUDED",
                         "exclusion_reason": issue, "label_schema": LABEL_SCHEMA,
                         "label": None if issue else {
                             "net_return": outcome["net_pnl"] / outcome["entry_notional"],
                             "profitable": outcome["net_pnl"] > 0, "known_at": outcome["known_at"],
                             "opened_at": outcome["opened_at"], "closed_at": outcome["closed_at"],
                             "outcome_id": outcome["outcome_id"]}}, now)
        return {"as_of": ts(now), "attachments_added": added, "decision_count": len(decisions),
                "execution": "NOT_SUBMITTED"}

    def _attach(self, identity: str, kind: str, source_id: str, payload: Mapping[str, Any],
                now: datetime) -> int:
        normalized = plain(payload)
        if kind != "ORDER":
            prior = [a for a in self.store.prospective.attachments(identity)
                     if a["kind"] == kind and a["source_id"] == source_id]
            if prior and any(a["payload"] != normalized for a in prior):
                raise ValueError("previously captured prospective source record changed")
        return int(self.store.prospective.save_attachment(
            {"decision_id": identity, "kind": kind, "source_id": source_id, "payload": normalized}, now))

    def _order_attachments(self, decision: Mapping[str, Any], order_id: str,
                           now: datetime, *, entry: bool) -> int:
        order = self.store.orders.get(order_id)
        execution = self.store.execution_records.for_order(order_id)
        if order is None or execution is None:
            raise ValueError("prospective link references a missing order/execution record")
        if not aware(decision["decision_at"]) <= aware(order["timestamp"]) <= now:
            raise ValueError("order timestamp outside prospective observation")
        if not aware(decision["decision_at"]) <= aware(execution["decision_timestamp"]) <= now \
                or execution["data_timestamp"] is not None \
                and aware(execution["data_timestamp"]) > aware(execution["decision_timestamp"]):
            raise ValueError("execution evidence outside prospective observation")
        if entry:
            frozen = decision["strategy_catalog"].get(decision["selected_strategy"])
            if frozen is None or execution["strategy_name"] != decision["selected_strategy"] \
                    or execution["strategy_version"] != frozen["version"] \
                    or execution["parameters_hash"] != frozen["parameters_hash"] \
                    or execution["timeframe"] != frozen["timeframe"]:
                raise ValueError("paper execution strategy version differs from frozen prospective decision")
        added = self._attach(decision["decision_id"], "EXECUTION", order_id, execution, now)
        added += self._attach(decision["decision_id"], "ORDER", order_id,
                              {"order": order, "execution": execution, "entry": entry}, now)
        for event in self.store.orders.events(order_id):
            if not aware(decision["decision_at"]) <= aware(event["timestamp"]) <= now:
                raise ValueError("future/predecision order event")
            added += self._attach(decision["decision_id"], "ORDER_EVENT", event["event_id"], event, now)
        for fill in self.store.fills.for_order(order_id):
            if not aware(decision["decision_at"]) <= aware(fill["timestamp"]) <= now:
                raise ValueError("future/predecision fill")
            added += self._attach(decision["decision_id"], "FILL", fill["fill_id"], fill, now)
        return added

    def _outcome_issue(self, decision: Mapping[str, Any], outcome: Mapping[str, Any],
                       fills: Mapping[str, Any], entry_orders: set[str]) -> str | None:
        if outcome["attribution_status"] != "COMPLETE":
            return "INCOMPLETE_OR_MIXED_ATTRIBUTION"
        frozen = decision["strategy_catalog"].get(decision["selected_strategy"])
        attribution = outcome["attribution"]
        if frozen is None or attribution["strategy_id"] != decision["selected_strategy"] \
                or attribution["strategy_version"] != frozen["version"] \
                or attribution["parameters_hash"] != frozen["parameters_hash"] \
                or attribution["timeframe"] != frozen["timeframe"]:
            return "OUTCOME_VERSION_MISMATCH"
        if outcome["instrument_id"] != decision["instrument_id"] or outcome["currency"] != decision["currency"] \
                or aware(outcome["opened_at"]) < aware(decision["decision_at"]):
            return "EPISODE_PREDATES_OR_DIFFERS_FROM_DECISION"
        if any(f not in fills for f in outcome["fill_ids"]):
            return "MISSING_EPISODE_FILL"
        entry_side = "BUY" if outcome["side"] == "LONG" else "SELL"
        if any(fills[f]["side"] == entry_side and fills[f]["client_order_id"] not in entry_orders
               for f in outcome["fill_ids"]):
            return "EPISODE_SHARED_WITH_OTHER_DECISIONS"
        if any(isinstance(outcome[key], bool) or not isinstance(outcome[key], (int, float))
               or not isfinite(outcome[key])
               for key in ("entry_notional", "gross_pnl", "fees", "net_pnl")) \
                or outcome["entry_notional"] <= 0 or outcome["fees"] < 0 \
                or abs(outcome["gross_pnl"] - outcome["fees"] - outcome["net_pnl"]) > 1e-7:
            raise ValueError("invalid outcome net accounting")
        if not aware(outcome["opened_at"]) <= aware(outcome["closed_at"]) <= aware(outcome["known_at"]):
            raise ValueError("invalid outcome label timestamps")
        return None

    def observation(self, identity: str) -> dict[str, Any]:
        decision = self.store.prospective.decision(identity)
        if decision is None:
            raise KeyError(identity)
        attachments = self.store.prospective.attachments(identity)
        labels = [a["payload"]["label"] for a in attachments
                  if a["kind"] == "OUTCOME" and a["payload"]["label_status"] == "ELIGIBLE"]
        for attachment in attachments:
            if attachment["kind"] == "OUTCOME" \
                    and attachment["payload"]["label_schema"] != decision["label_schema"]:
                raise ValueError("prospective outcome label schema differs from decision")
        for label in labels:
            value = label["net_return"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) \
                    or not isinstance(label["profitable"], bool) or label["profitable"] != (value > 0) \
                    or not aware(decision["decision_at"]) <= aware(label["opened_at"]) \
                    <= aware(label["closed_at"]) <= aware(label["known_at"]):
                raise ValueError("invalid prospective outcome label")
        # Replays never multiply labels; changing a source after initial capture is a conflict.
        by_outcome = {label["outcome_id"]: label for label in labels}
        orders = [a["payload"] for a in attachments if a["kind"] == "ORDER" and a["payload"]["entry"]]
        if by_outcome:
            status = "LABELED_CLOSED_EPISODE"
        elif any(a["kind"] == "OUTCOME" for a in attachments):
            status = "OUTCOME_EXCLUDED"
        elif any(a["kind"] == "FILL" for a in attachments):
            status = "FILLED_AWAITING_CLOSED_OUTCOME"
        elif orders:
            status = "UNFILLED_OR_REJECTED_ORDER"
        elif any(a["kind"] == "RISK" and a["payload"]["status"] == "RISK_REJECTED" for a in attachments):
            status = "RISK_REJECTED"
        else:
            status = "NO_TRADE" if decision["selected_strategy"] == "NO_TRADE" else "NO_EXECUTION_OBSERVED"
        return {"decision": decision, "attachments": attachments, "status": status,
                "labels": sorted(by_outcome.values(), key=lambda label: label["outcome_id"]),
                "censored": not bool(by_outcome)}

    def readiness(self, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read-only assessment; thresholds are necessary gates, never permission to train/promote."""
        observations = [self.observation(d["decision_id"]) for d in self.store.prospective.decisions()]
        missing: dict[str, int] = {}
        for observation in observations:
            for name, feature in observation["decision"]["features"].items():
                if feature["value"] is None:
                    missing[name] = missing.get(name, 0) + 1
        fixture_count = sum(fixture_source(o["decision"]["market_data_provider"]) for o in observations)
        partitions = manifest["partitions"] if manifest else {}
        counts = {name: len(partitions.get(name, [])) for name in ("TRAIN", "VALIDATION", "TEST", "FINAL_HOLDOUT")}
        blockers = ["SOURCE_PUBLICATION_VINTAGE_AND_REAL_DATA_ORIGIN_NOT_INDEPENDENTLY_ATTESTED",
                    "SELECTED_EXECUTED_TRADES_ONLY_NO_COUNTERFACTUAL_LABELS",
                    "PREREGISTERED_PROSPECTIVE_CONTROL_EXPERIMENT_REQUIRED"]
        if fixture_count:
            blockers.append("FIXTURE_OR_SYNTHETIC_CAPTURES_NOT_REAL_MARKET_TRAINING_EVIDENCE")
        if missing:
            blockers.append("MISSING_TIMESTAMPED_FEATURE_VALUES")
        if not manifest:
            blockers.append("NO_FROZEN_CHRONOLOGICAL_DATASET")
        for name, minimum in (("TRAIN", 100), ("VALIDATION", 30), ("TEST", 30)):
            rows = partitions.get(name, [])
            if len(rows) < minimum:
                blockers.append(f"{name}_REQUIRES_{minimum}_ELIGIBLE_LABELED_DECISIONS")
            if len({r["label"]["profitable"] for r in rows}) < 2:
                blockers.append(f"{name}_MISSING_BOTH_LABEL_CLASSES")
        return {"status": "NOT_READY", "mode": "ADVISORY_ONLY", "feature_schema": FEATURE_SCHEMA,
                "label_schema": LABEL_SCHEMA, "captured_decisions": len(observations),
                "labeled_decisions": sum(bool(o["labels"]) for o in observations),
                "censored_decisions": sum(o["censored"] for o in observations),
                "fixture_capture_count": fixture_count, "missing_feature_counts": missing,
                "unattested_source_count": len(observations),
                "label_definition": "Actual fully closed selected paper episode net PnL / entry notional; "
                    "profitable iff net PnL > 0. Rejections/unfilled/NO_TRADE are censored, not zero.",
                "required_feature_fields": list(FEATURE_FIELDS),
                "partition_counts": counts, "blockers": blockers, "training": "NOT_PERFORMED",
                "risk_authority": "UNCHANGED_RISK_ENGINE"}

    def create_dataset(
        self, *, as_of: datetime, train_end: datetime, validation_end: datetime,
        test_end: datetime, final_end: datetime, embargo_hours: int = 24,
    ) -> dict[str, Any]:
        with self.store.db.transaction():
            return self._create_dataset(
                as_of=as_of, train_end=train_end, validation_end=validation_end,
                test_end=test_end, final_end=final_end, embargo_hours=embargo_hours)

    def _create_dataset(
        self, *, as_of: datetime, train_end: datetime, validation_end: datetime,
        test_end: datetime, final_end: datetime, embargo_hours: int,
    ) -> dict[str, Any]:
        now = aware(self.clock())
        for at in (as_of, train_end, validation_end, test_end, final_end):
            aware(at)
        if not train_end < validation_end < test_end < final_end <= as_of <= now \
                or isinstance(embargo_hours, bool) or not isinstance(embargo_hours, int) \
                or not 1 <= embargo_hours <= 720:
            raise ValueError("dataset requires chronological completed boundaries and positive embargo")
        policy = {"as_of": ts(as_of), "train_end": ts(train_end), "validation_end": ts(validation_end),
                  "test_end": ts(test_end), "final_end": ts(final_end), "embargo_hours": embargo_hours,
                  "feature_schema": FEATURE_SCHEMA, "label_schema": LABEL_SCHEMA,
                  "version": "prospective-selected-episode-dataset-v1"}
        identity = ProspectiveRepository.digest(policy)
        prior = self.store.prospective.manifest(identity)
        if prior is not None:
            return prior
        self.reconcile()
        partitions = {name: [] for name in ("TRAIN", "VALIDATION", "TEST", "FINAL_HOLDOUT")}
        exclusions = []
        embargo = timedelta(hours=embargo_hours)
        assigned = self.store.prospective.assigned()
        for decision in self.store.prospective.decisions():
            did, at = decision["decision_id"], aware(decision["decision_at"])
            if at > as_of:
                continue
            if did in assigned:
                exclusions.append({"decision_id": did, "reason": "ALREADY_ASSIGNED_TO_FROZEN_DATASET"})
                continue
            observation = self.observation(did)
            reason = None
            if decision["feature_schema"] != FEATURE_SCHEMA or decision["label_schema"] != LABEL_SCHEMA:
                reason = "SCHEMA_VERSION_MISMATCH"
            elif set(decision["features"]) != set(FEATURE_FIELDS):
                reason = "FEATURE_FIELDS_MISMATCH"
            elif decision["feature_hash"] != ProspectiveRepository.digest(decision["features"]):
                reason = "FEATURE_HASH_MISMATCH"
            elif decision["selected_strategy"] == "NO_TRADE" or len(observation["labels"]) != 1:
                reason = "CENSORED_OR_MULTIPLE_EPISODES_NOT_A_SINGLE_TRADE_LABEL"
            elif any(v["value"] is None for v in decision["features"].values()):
                reason = "INCOMPLETE_FEATURE_VECTOR"
            elif any(isinstance(v["value"], bool) or not isinstance(v["value"], (int, float))
                     or not isfinite(v["value"]) for v in decision["features"].values()):
                reason = "INVALID_FEATURE_VALUE"
            elif fixture_source(decision["market_data_provider"]):
                reason = "FIXTURE_OR_SYNTHETIC_CAPTURE"
            elif any(aware(v["available_at"]) > at for v in decision["features"].values()):
                reason = "FEATURE_NOT_AVAILABLE_AT_DECISION"
            if reason:
                exclusions.append({"decision_id": did, "reason": reason})
                continue
            label = observation["labels"][0]
            partition = None
            for name, lo, hi in (("TRAIN", None, train_end),
                ("VALIDATION", train_end + embargo, validation_end),
                ("TEST", validation_end + embargo, test_end),
                ("FINAL_HOLDOUT", test_end + embargo, final_end)):
                if (lo is None or at >= lo) and at <= hi:
                    if aware(label["known_at"]) <= hi and aware(label["closed_at"]) <= hi:
                        partition = name
                    break
            reserved = self.store.strategy_lab.reserved_windows(decision["instrument_id"])
            if partition != "FINAL_HOLDOUT" and any(
                aware(decision["research_as_of"]) <= aware(r["end"])
                and aware(label["closed_at"]) >= aware(r["start"]) for r in reserved):
                partition = None
            if partition is None:
                exclusions.append({"decision_id": did, "reason": "EMBARGO_LABEL_BOUNDARY_OR_RESERVED_HOLDOUT"})
                continue
            row = {"decision_id": did, "decision_hash": ProspectiveRepository.digest(decision),
                   "feature_hash": decision["feature_hash"], "features": decision["features"],
                   "strategy": decision["strategy_catalog"][decision["selected_strategy"]],
                   "market_data_provider": decision["market_data_provider"],
                   "source_verification": decision["source_verification"],
                   "decision_at": decision["decision_at"], "label": label}
            row["row_hash"] = ProspectiveRepository.digest(row)
            partitions[partition].append(row)
        manifest = {"manifest_id": identity, "created_at": ts(now), "policy": policy, "partitions": partitions,
                    "exclusions": exclusions, "evidence_origin": "PROSPECTIVE_CAPTURE_ONLY",
                    "final_holdout_policy": "LOCKED_REVIEW_ONLY_NEVER_TRAIN_OR_TUNE",
                    "dataset_hash": ProspectiveRepository.digest({"policy": policy, "partitions": partitions}),
                    "limitations": ["Readiness is not training permission; source origin requires independent verification."]}
        manifest["readiness"] = self.readiness(manifest)
        self.store.prospective.save_manifest(manifest, now)
        result = self.store.prospective.manifest(identity)
        if result is None:
            raise RuntimeError("prospective dataset was not persisted")
        return result
