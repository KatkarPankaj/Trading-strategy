"""Preregistered paper experiment protocols; registration never authorizes execution."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .executors import TradingMode
from .persistence import Store
from .persistence.repositories import ProspectiveRepository, ts
from .prospective_evidence import aware


class PaperExperimentService:
    def __init__(self, store: Store, *, mode: TradingMode,
                 clock: Callable[[], datetime]) -> None:
        if mode is not TradingMode.PAPER:
            raise ValueError("experiments require PAPER mode")
        self.store = store
        self.clock = clock

    def register(
        self, *, experiment_id: str, start_date: date, duration_days: int,
        operator: str, acceptance_policy_ref: str, runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = aware(self.clock())
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", experiment_id):
            raise ValueError("invalid experiment identity")
        if isinstance(start_date, datetime) or not isinstance(start_date, date):
            raise ValueError("start_date must be a calendar date")
        if type(duration_days) is not int or not 1 <= duration_days <= 366:
            raise ValueError("duration_days must be from 1 to 366 calendar days")
        if not operator.strip() or not acceptance_policy_ref.strip():
            raise ValueError("operator and acceptance policy reference are required")
        if runtime.get("mode") != "PAPER" or runtime.get("single_market") is not True:
            raise ValueError("experiment requires a single-market PAPER runtime")
        zone = ZoneInfo(runtime["timezone"])
        start = datetime.combine(start_date, time.min, zone)
        end = datetime.combine(start_date + timedelta(days=duration_days), time.min, zone)
        policy = {
            "version": "paper-experiment-protocol-v1", "mode": "PAPER",
            "start_date": start_date.isoformat(), "duration_calendar_days": duration_days,
            "starts_at": ts(start.astimezone(timezone.utc)),
            "ends_at_exclusive": ts(end.astimezone(timezone.utc)),
            "operator": operator, "acceptance_policy_ref": acceptance_policy_ref,
            "acceptance_policy_status": "OPERATOR_REFERENCE_NOT_INDEPENDENTLY_VERIFIED",
            "runtime": dict(runtime),
            "promotion": "EXPLICIT_REVIEW_ONLY_NO_AUTOMATIC_PROMOTION",
        }
        digest = ProspectiveRepository.digest(policy)
        with self.store.db.transaction():
            prior = self.store.prospective.experiment(experiment_id)
            if prior is not None:
                if prior["protocol_hash"] != digest:
                    raise ValueError("experiment identity already binds a different protocol")
                return self.status(experiment_id)
            if start <= now:
                raise ValueError("experiment must be preregistered before its start; no backfill")
            self.store.prospective.save_experiment({
                "experiment_id": experiment_id, "created_at": ts(now),
                "protocol": policy, "protocol_hash": digest,
            })
        return self.status(experiment_id)

    def status(self, experiment_id: str) -> dict[str, Any]:
        record = self.store.prospective.experiment(experiment_id)
        if record is None:
            raise KeyError("unknown paper experiment")
        policy = record["protocol"]
        if ProspectiveRepository.digest(policy) != record["protocol_hash"]:
            raise ValueError("experiment protocol integrity check failed")
        now = aware(self.clock())
        start, end = aware(policy["starts_at"]), aware(policy["ends_at_exclusive"])
        phase = "SCHEDULED" if now < start else (
            "EVALUATION_DUE" if now >= end else "OBSERVATION_WINDOW")
        return {
            **record, "as_of": ts(now), "phase": phase,
            "execution": "NOT_AUTHORIZED_BY_REGISTRATION",
            "evaluation": "NOT_PERFORMED", "training": "NOT_PERFORMED",
            "blockers": [
                "EXPERIMENT_CYCLE_BINDING_NOT_IMPLEMENTED",
                "DAILY_FINANCIAL_AND_BENCHMARK_CHECKPOINTS_NOT_IMPLEMENTED",
                "FINAL_EVALUATION_NOT_IMPLEMENTED",
            ],
        }
