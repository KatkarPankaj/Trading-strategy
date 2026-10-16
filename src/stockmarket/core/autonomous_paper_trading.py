"""Bounded, durable orchestration of the PAPER research-to-order workflow."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from .adaptive_discovery import (
    AdaptiveDiscoverySettings, plan_rotation, protected_instruments, record_added)
from .executors import TradingMode
from .autonomous_research import IdempotencyConflict
from .paper_lifecycle import PaperLifecycleError
from .persistence.repositories import to_json


class AutonomousPaperCycleError(RuntimeError):
    """An autonomous paper cycle cannot safely start or resume."""


class AutonomousPaperTradingService:
    def __init__(
        self,
        *,
        repository: Any,
        research: Any,
        signals: Any,
        proposals: Any,
        execution: Any,
        positions: Any,
        gate: Any,
        account_id: str = "PAPER",
        max_candidates: int = 10,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
        heartbeat_interval: float = 30.0,
        monitoring_check: Callable[[], dict[str, Any]] | None = None,
        discovery: AdaptiveDiscoverySettings | None = None,
        exploratory: bool = False,
    ) -> None:
        if execution.trading.mode is not TradingMode.PAPER:
            raise ValueError("autonomous paper cycles require PAPER execution")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
                or not 1 <= max_candidates <= 100:
            raise ValueError("max_candidates must be between 1 and 100")
        if not account_id.strip():
            raise ValueError("account_id must be non-empty")
        if lease_duration <= timedelta(0) or heartbeat_interval <= 0:
            raise ValueError(
                "cycle lease and heartbeat intervals must be positive")
        self.repository = repository
        self.research = research
        self.signals = signals
        self.proposals = proposals
        self.execution = execution
        self.positions = positions
        self.monitoring_check = monitoring_check
        self.discovery = discovery
        if not isinstance(exploratory, bool):
            raise ValueError("exploratory must be a bool")
        # Opt-in: unvalidated strategies may enter PAPER positions; every gate still applies and
        # every cycle is labelled EXPLORATORY_UNVALIDATED. It never qualifies a strategy.
        self.exploratory = exploratory
        self.gate = gate
        self.account_id = account_id
        self.max_candidates = max_candidates
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_duration = lease_duration
        self.heartbeat_interval = heartbeat_interval
        self._active_lock = threading.RLock()
        self._active: set[str] = set()

    def run(
        self,
        universe_id: str,
        *,
        idempotency_key: str,
        operator: str,
        as_of: datetime | None = None,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(universe_id, str) or not universe_id.strip():
            raise ValueError("universe_id must be non-empty")
        self._validate_identifier(idempotency_key, "idempotency_key")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be non-empty")
        count = self.max_candidates if top_n is None else top_n
        if isinstance(count, bool) or not isinstance(count, int) \
                or not 1 <= count <= self.max_candidates:
            raise ValueError(
                f"top_n must be between 1 and {self.max_candidates}")
        prior = self.repository.get_by_key(idempotency_key)
        now = self._now()
        timestamp = self._aware(
            as_of if as_of is not None else (
                datetime.fromisoformat(prior["as_of"])
                if prior is not None else now),
            "as_of",
        )
        if timestamp > now:
            raise ValueError("as_of cannot be in the future")
        timestamp = timestamp.astimezone(timezone.utc)

        if prior is not None:
            request_hash = self._request_hash(
                universe_id, timestamp, count, operator)
            if prior["request_hash"] != request_hash:
                raise AutonomousPaperCycleError(
                    "idempotency_key was already used for a different paper cycle")
            if prior["status"] in {"COMPLETE", "PARTIAL", "FAILED"}:
                return self.get_run(prior["run_id"])
            raise AutonomousPaperCycleError(
                "paper cycle is already in progress; use the recovery operation after restart")

        run_id = uuid5(
            NAMESPACE_URL, f"stockmarket:paper-cycle:{idempotency_key}").hex
        owner_token = uuid4().hex
        request = {
            "universe_id": universe_id,
            "idempotency_key": idempotency_key,
            "operator": operator,
            "mode": TradingMode.PAPER.value,
            "as_of": timestamp.isoformat(),
            "top_n": count,
        }
        payload = {
            "request": request,
            "position_management": None,
            "research_run_id": None,
            "completed_candidates": 0,
            "failed_candidates": 0,
        }
        request_hash = self._request_hash(
            universe_id, timestamp, count, operator)
        try:
            self.repository.create(
                run_id=run_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                universe_id=universe_id,
                market_scope=self.account_id,
                account_id=self.account_id,
                as_of=timestamp,
                payload=payload,
                owner_token=owner_token,
                lease_until=self._lease_until(),
                now=now,
            )
        except ValueError as exc:
            raise AutonomousPaperCycleError(str(exc)) from exc
        except Exception as exc:
            existing = self.repository.get_by_key(idempotency_key)
            if existing is None:
                raise
            if existing["request_hash"] != request_hash:
                raise AutonomousPaperCycleError(
                    "idempotency_key was already used for a different paper cycle") from exc
            if existing["status"] in {"COMPLETE", "PARTIAL", "FAILED"}:
                return self.get_run(existing["run_id"])
            raise AutonomousPaperCycleError(
                "paper cycle is already in progress") from exc
        return self._execute(run_id, owner_token, recovering=False)

    def recover(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown autonomous paper cycle {run_id!r}")
        if run["status"] in {"COMPLETE", "FAILED"}:
            return self.get_run(run_id)
        owner_token = uuid4().hex
        if not self.repository.acquire_recovery(
            run_id,
            owner_token=owner_token,
            lease_until=self._lease_until(),
            now=self._now(),
        ):
            return self.get_run(run_id)
        return self._execute(run_id, owner_token, recovering=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.repository.get_run(run_id)
        if run is None:
            return None
        return {
            "run_id": run["run_id"],
            "idempotency_key": run["idempotency_key"],
            "mode": run["mode"],
            "status": run["status"],
            "stage": run["stage"],
            "universe_id": run["universe_id"],
            "market_scope": run["market_scope"],
            "account_id": run["account_id"],
            "as_of": run["as_of"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "payload": run["payload"],
            "candidates": self.repository.candidates(run_id),
            "events": self.repository.events(run_id),
            "execution": "PAPER",
        }

    def _execute(
        self,
        run_id: str,
        owner_token: str,
        *,
        recovering: bool,
    ) -> dict[str, Any]:
        with self._active_lock:
            if run_id in self._active:
                raise AutonomousPaperCycleError(
                    "paper cycle is already active in this process")
            self._active.add(run_id)
        stop_heartbeat = threading.Event()
        lease_lost = threading.Event()
        lease_error: list[str] = []
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(run_id, owner_token, stop_heartbeat, lease_lost, lease_error),
            name=f"paper-cycle-lease-{run_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            run = self.repository.get_run(run_id)
            if run is None:
                raise AutonomousPaperCycleError("paper cycle disappeared")
            payload = dict(run["payload"])
            request = payload["request"]
            self.repository.update_run(
                run_id, status="RECOVERING" if recovering else "RUNNING",
                stage="MANAGING_POSITIONS", payload=payload,
                owner_token=owner_token)
            self._event(run_id, None, "CYCLE_STARTED", {
                "recovering": recovering, "mode": TradingMode.PAPER.value})
            payload["position_management"] = (
                self.monitoring_check() if self.monitoring_check is not None else self.positions.manage())
            self._assert_lease(run_id, owner_token, lease_lost, lease_error)
            if any(row.get("status") in {"MONITOR_FAILED", "RECONCILE_FAILED", "FAILED", "REJECTED"}
                   for row in (*payload["position_management"].get("orders", []),
                               *payload["position_management"].get("positions", []))):
                payload["failure"] = "position/order monitoring failed; no entries attempted"
                return self._finish(run_id, payload, "PARTIAL", "MONITORING_FAILED", owner_token)

            if self.gate.halted:
                payload["halt_reason"] = self.gate.blocked_reason()
                return self._finish(
                    run_id, payload, "PARTIAL", "HALTED", owner_token)

            history: list[dict[str, Any]] = []
            if self.discovery is not None and "discovery" not in payload:
                # Planned once and persisted, so recovery reuses the same research request.
                history = [
                    {**row, "candidates": self.repository.candidates(row["run_id"])}
                    for row in self.repository.recent(self.discovery.history_cycles)
                    if row["run_id"] != run_id]
                payload["discovery"] = plan_rotation(
                    history, protected=protected_instruments(payload["position_management"]),
                    now=datetime.fromisoformat(request["as_of"]), settings=self.discovery)
                self._checkpoint_payload(run_id, payload, "DISCOVERY_PLANNED", owner_token)
            if "selection_scope" not in payload:
                payload["selection_scope"] = (
                    "EXPLORATORY_UNVALIDATED" if self.exploratory else "VALIDATED_AUTOMATIC")
            deprioritized = tuple((payload.get("discovery") or {}).get("deprioritized") or ())
            try:
                research_result = self.research.run(
                    request["universe_id"],
                    idempotency_key="paper-" + hashlib.sha256(
                        request["idempotency_key"].encode()).hexdigest(),
                    mode=TradingMode.PAPER.value,
                    as_of=datetime.fromisoformat(request["as_of"]),
                    top_n=request["top_n"],
                    validated_selection=payload["selection_scope"] != "EXPLORATORY_UNVALIDATED",
                    **({"deprioritized": deprioritized} if deprioritized else {}),
                )
            except IdempotencyConflict as exc:
                if not recovering:
                    raise
                payload["recovery_disposition"] = {
                    "reason": "RESEARCH_CONFIGURATION_CHANGED",
                    "error": str(exc),
                    "at": self._now().isoformat(),
                    "entries_retried": False,
                }
                return self._finish(
                    run_id, payload, "FAILED", "RECOVERY_OBSOLETE", owner_token)
            evaluation_as_of = self._now()
            research_run_id = str(research_result["run_id"])
            payload["research_run_id"] = research_run_id
            self.repository.update_run(
                run_id, status="RUNNING", stage="RESEARCH_COMPLETE",
                payload=payload, owner_token=owner_token)
            self._assert_lease(run_id, owner_token, lease_lost, lease_error)
            selected = [
                candidate for candidate in research_result.get("candidates", [])
                if candidate.get("stage") == "STRATEGY_SELECTED"
            ][:request["top_n"]]
            selected_ids = {str(item["instrument_id"]) for item in selected}
            if history and payload.get("discovery") is not None:
                record_added(payload["discovery"], history,
                             research_result.get("candidates", []), self._now().isoformat())
            for candidate in research_result.get("candidates", []):
                candidate_id = str(candidate.get("instrument_id", ""))
                if not candidate_id:
                    continue
                if candidate_id not in selected_ids:
                    assessment = (candidate.get("opportunity") or {}).get("assessment") or {}
                    self._save_candidate(
                        run_id, candidate_id, status="RESEARCH_REJECTED",
                        payload={
                            "instrument_id": candidate_id,
                            "research_stage": candidate.get("stage"),
                            "research_assessment": assessment,
                            "research_error": ("CANDIDATE_LIMIT: selected candidate exceeds this cycle's top_n"
                            if candidate.get("stage") == "STRATEGY_SELECTED" else
                            candidate.get("error") or assessment.get("error")
                            or "; ".join(assessment.get("rejection_reasons") or [])
                            or assessment.get("strategy_reason")),
                        })
            self._checkpoint_payload(
                run_id, payload, "PROCESSING_CANDIDATES", owner_token)

            failures = sum(
                1 for candidate in research_result.get("candidates", [])
                if candidate.get("stage") == "FAILED")
            completed = 0
            for candidate in selected:
                self._assert_lease(run_id, owner_token, lease_lost, lease_error)
                candidate_id = str(candidate["instrument_id"])
                previous = self.repository.get_candidate(run_id, candidate_id)
                if previous is not None and previous["status"] not in {
                    "FAILED", "IN_PROGRESS",
                }:
                    if previous["status"] == "COMPLETE":
                        completed += 1
                    continue
                try:
                    outcome = self._process_candidate(
                        run_id=run_id,
                        owner_token=owner_token,
                        research_run_id=research_run_id,
                        candidate_id=candidate_id,
                        operator=request["operator"],
                        as_of=evaluation_as_of,
                        lease_lost=lease_lost,
                        lease_error=lease_error,
                        research_candidate=candidate,
                    )
                    self._save_candidate(
                        run_id, candidate_id, status=outcome["status"],
                        payload=outcome)
                    self._event(
                        run_id, candidate_id, "CANDIDATE_CHECKPOINT", outcome)
                    if outcome["status"] == "COMPLETE":
                        completed += 1
                    elif outcome["status"] in {"FAILED", "ORDER_REJECTED"}:
                        failures += 1
                except Exception as exc:
                    if lease_lost.is_set():
                        raise AutonomousPaperCycleError(
                            "; ".join(lease_error) or "paper cycle lease was lost")
                    failures += 1
                    outcome = {
                        "instrument_id": candidate_id,
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                        "execution": "PAPER",
                    }
                    self._save_candidate(
                        run_id, candidate_id, status="FAILED", payload=outcome)
                    self._event(
                        run_id, candidate_id, "CANDIDATE_FAILED", outcome)

            payload["position_management"] = self.positions.manage()
            self._assert_lease(run_id, owner_token, lease_lost, lease_error)
            payload["completed_candidates"] = completed
            payload["failed_candidates"] = failures
            status = "PARTIAL" if failures else "COMPLETE"
            return self._finish(
                run_id, payload, status, "COMPLETE", owner_token)
        except Exception as exc:
            run = self.repository.get_run(run_id)
            payload = dict(run["payload"]) if run is not None else {}
            payload["failure"] = f"{type(exc).__name__}: {exc}"
            if not lease_lost.is_set():
                self._event(run_id, None, "CYCLE_FAILED", payload)
                self.repository.update_run(
                    run_id, status="PARTIAL", stage="INTERRUPTED",
                    payload=payload, owner_token=owner_token)
            raise AutonomousPaperCycleError(
                f"paper cycle {run_id} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_interval * 2))
            with self._active_lock:
                self._active.discard(run_id)

    def _process_candidate(
        self,
        *,
        run_id: str,
        owner_token: str,
        research_run_id: str,
        candidate_id: str,
        operator: str,
        as_of: datetime,
        lease_lost: threading.Event,
        lease_error: list[str],
        research_candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        assessment = (research_candidate.get("opportunity") or {}).get("assessment") or {}
        self._save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={"instrument_id": candidate_id, "stage": "SIGNAL_GENERATION",
                     "research_assessment": assessment, "research_stage": "STRATEGY_SELECTED"})
        signal_result = self.signals.generate(
            research_run_id, candidate_id, evaluation_as_of=as_of)
        self._assert_lease(run_id, owner_token, lease_lost, lease_error)
        outcome: dict[str, Any] = {
            "instrument_id": candidate_id,
            "signal": signal_result,
            "execution": "PAPER",
        }
        if signal_result.get("status") != "SIGNAL_GENERATED":
            outcome["status"] = "SIGNAL_REJECTED"
            outcome["error"] = signal_result.get(
                "reason", "signal generation rejected")
            return outcome

        self._save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={**outcome, "stage": "RISK_EVALUATION"})
        # Risk is evaluated after the signal is persisted; reusing the signal's evaluation time
        # made its own generated_at look future-dated and rejected every actionable signal.
        risk_result = self.proposals.evaluate(
            research_run_id, candidate_id, evaluation_as_of=self._now())
        self._assert_lease(run_id, owner_token, lease_lost, lease_error)
        outcome["risk"] = risk_result
        proposal = risk_result.get("trade_proposal")
        if risk_result.get("status") != "RISK_APPROVED" or not proposal:
            outcome["status"] = (
                "NON_ACTIONABLE"
                if risk_result.get("status") == "NON_ACTIONABLE"
                else "RISK_REJECTED")
            return outcome

        proposal_id = str(proposal["proposal_id"])
        outcome["proposal_id"] = proposal_id
        self._save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={**outcome, "stage": "ORDER_SUBMISSION"})
        try:
            execution_result = self.execution.submit(
                proposal_id,
                operator=operator,
                sizing_mode="AUTOMATIC_SIZING",
                quantity=None,
            )
        except PaperLifecycleError as exc:
            outcome["status"] = "EXECUTION_REJECTED"
            outcome["error"] = str(exc)
            outcome["rejection_code"] = exc.code
            outcome["execution_quote"] = exc.details.get("execution_quote")
            return outcome
        self._assert_lease(run_id, owner_token, lease_lost, lease_error)
        order = execution_result["order"]
        outcome["order"] = {
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
            "error": order.error,
        }
        outcome["execution_quote"] = execution_result.get("execution_quote")
        outcome["execution_risk"] = execution_result.get("risk_decision")
        outcome["status"] = (
            "COMPLETE" if order.status.value in {
                "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED",
            } else "ORDER_CANCELLED" if order.status.value == "CANCELLED"
            else "ORDER_REJECTED")
        return outcome

    def _save_candidate(
        self, run_id: str, candidate_id: str, *, status: str, payload: Mapping[str, Any],
    ) -> None:
        previous = self.repository.get_candidate(run_id, candidate_id)
        prior_payload = dict(previous["payload"]) if previous else {}
        if status == "IN_PROGRESS" and payload.get("stage") == "SIGNAL_GENERATION":
            for key in ("error", "rejection_code", "signal", "risk", "order",
                        "execution_quote", "execution_risk"):
                prior_payload.pop(key, None)
        material = {**prior_payload, **json.loads(to_json(payload)), "status": status}
        material["decision_trace"] = self._decision_trace(run_id, candidate_id, status, material)
        self.repository.save_candidate(run_id, candidate_id, status=status, payload=material)

    def _decision_trace(
        self, run_id: str, candidate_id: str, status: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        generation = payload.get("signal") or {}
        signal = generation.get("signal") or {}
        provenance = generation.get("provenance") or {}
        assessment = payload.get("research_assessment") or {}
        risk = payload.get("risk") or {}
        order = payload.get("order") or {}
        strategy = signal.get("strategy") or provenance.get("strategy") or assessment.get("recommended_strategy")
        catalog = (assessment.get("input_context") or {}).get("registered_strategies") or []
        metadata = next((item for item in catalog if item.get("name") == strategy), {})
        instrument = getattr(self.signals, "instruments", {}).get(candidate_id)
        reason = payload.get("error") or payload.get("research_error") or risk.get("reason")
        final = "PENDING"
        code = None
        if status == "NON_ACTIONABLE":
            final = "NO_TRADE"
            reason = generation.get("reason") or reason
            code = next(iter(signal.get("reasons") or []), None)
        elif status == "RESEARCH_REJECTED":
            final = "NO_TRADE" if assessment.get("status") == "NO_TRADE" else "REJECTED"
            code = "RESEARCH_NO_TRADE" if final == "NO_TRADE" else (
                assessment.get("status") or "RESEARCH_REJECTED")
            reason = reason or "Candidate was not selected for deterministic strategy evaluation."
        elif status in {"SIGNAL_REJECTED", "RISK_REJECTED", "EXECUTION_REJECTED", "FAILED"}:
            final = "BLOCKED" if status in {"EXECUTION_REJECTED", "FAILED"} else "REJECTED"
            if status == "SIGNAL_REJECTED":
                reason = generation.get("reason") or reason
        elif order:
            final = {
                "FILLED": "FILLED", "SUBMITTED": "SUBMITTED", "ACCEPTED": "SUBMITTED",
                "PARTIALLY_FILLED": "SUBMITTED", "NEW": "PENDING", "VALIDATED": "PENDING",
                "CANCELLED": "CANCELLED",
            }.get(order.get("status"), "REJECTED")
            reason = order.get("error") if final == "REJECTED" else None
            if final == "CANCELLED":
                reason = order.get("error") or "Paper order cancelled; any recorded fills remain in the ledger."
        if final in {"NO_TRADE", "REJECTED", "BLOCKED"}:
            head = str(reason or "").split(":", 1)[0]
            code = payload.get("rejection_code") or code or (
                head if head and all(c.isupper() or c.isdigit() or c == "_" for c in head)
                else f"{status}_BLOCKER")
            reason = reason or f"Candidate stopped at {status}; no paper submission was authorized."
            if reason == code:
                reason = f"{code}: {str(code).replace('_', ' ').capitalize()}."
            if generation.get("detail") and status == "SIGNAL_REJECTED":
                reason += f" {generation['detail']}"
        gates = [
            {"gate": "research_eligibility", "outcome": payload.get("research_stage", "NOT_EVALUATED"),
             "reason": payload.get("research_error")},
            {"gate": "strategy_signal_and_registration",
             "outcome": generation.get("status", "NOT_EVALUATED"),
             "reason": generation.get("reason"), "provenance": provenance},
            {"gate": "proposal_risk_and_sizing", "outcome": risk.get("status", "NOT_EVALUATED"),
             "reason": risk.get("reason")},
            {"gate": "fresh_quote_session_and_final_execution_risk",
             "outcome": order.get("status") or (
                 "BLOCKED" if status == "EXECUTION_REJECTED" else "NOT_EVALUATED"),
             "reason": payload.get("error"), "quote": payload.get("execution_quote"),
             "risk_decision": payload.get("execution_risk")},
        ]
        run = self.repository.get_run(run_id) or {}
        return {
            "selection_scope": (run.get("payload") or {}).get("selection_scope", "VALIDATED_AUTOMATIC"),
            "candidate_id": candidate_id, "cycle_id": run_id,
            "symbol": signal.get("symbol") or (instrument.symbol if instrument else candidate_id),
            "strategy": strategy, "strategy_version": provenance.get("strategy_version")
            or metadata.get("version"), "decision_timestamp": self._now().isoformat(),
            "signal_result": {"status": generation.get("status", "NOT_EVALUATED"),
                              "intent": signal.get("intent"), "side": signal.get("side"),
                              "reason": generation.get("reason")},
            "gate_outcomes": gates, "final_status": final, "rejection_code": code,
            "reason": reason, "client_order_id": order.get("client_order_id"),
            "execution": "PAPER" if order else "NOT_SUBMITTED",
        }

    def _checkpoint_payload(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        stage: str,
        owner_token: str,
    ) -> None:
        self.repository.update_run(
            run_id, status="RUNNING", stage=stage, payload=payload,
            owner_token=owner_token)

    def _finish(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        status: str,
        stage: str,
        owner_token: str,
    ) -> dict[str, Any]:
        self.repository.update_run(
            run_id, status=status, stage=stage, payload=payload,
            owner_token=owner_token)
        self._event(run_id, None, "CYCLE_FINISHED", {
            "status": status, "stage": stage})
        result = self.get_run(run_id)
        if result is None:
            raise AutonomousPaperCycleError(
                "paper cycle disappeared after completion")
        return result

    def _event(
        self,
        run_id: str,
        candidate_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        now = self._now()
        nonce = uuid4().hex
        self.repository.record_event(
            event_id=uuid5(
                NAMESPACE_URL,
                f"paper-cycle-event:{run_id}:{candidate_id}:{event_type}:{now.isoformat()}:{nonce}",
            ).hex,
            run_id=run_id,
            candidate_id=candidate_id,
            event_type=event_type,
            correlation_id=run_id,
            payload=payload,
            timestamp=now,
        )

    def _heartbeat(
        self,
        run_id: str,
        owner_token: str,
        stop: threading.Event,
        lease_lost: threading.Event,
        lease_error: list[str],
    ) -> None:
        while not stop.wait(self.heartbeat_interval):
            try:
                if not self.repository.renew_lock(
                    run_id,
                    owner_token=owner_token,
                    lease_until=self._lease_until(),
                    now=self._now(),
                ):
                    lease_error.append("paper cycle execution lease was lost")
                    lease_lost.set()
                    return
            except Exception as exc:
                lease_error.append(
                    f"paper cycle lease renewal failed: {type(exc).__name__}: {exc}")
                lease_lost.set()
                return

    def _assert_lease(
        self,
        run_id: str,
        owner_token: str,
        lease_lost: threading.Event,
        lease_error: list[str],
    ) -> None:
        if not self.repository.owns_lock(run_id, owner_token, self._now()):
            lease_lost.set()
        if lease_lost.is_set():
            raise AutonomousPaperCycleError(
                "; ".join(lease_error) or "paper cycle execution lease was lost")

    def _now(self) -> datetime:
        return self._aware(self.clock(), "clock").astimezone(timezone.utc)

    def _lease_until(self) -> datetime:
        return self._now() + self.lease_duration

    @staticmethod
    def _aware(value: datetime, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value

    @staticmethod
    def _validate_identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 128 \
                or any(not (char.isascii() and (
                    char.isalnum() or char in "_-")) for char in value):
            raise ValueError(
                f"{name} must be a safe identifier of at most 128 characters")

    @staticmethod
    def _request_hash(
        universe_id: str,
        as_of: datetime,
        top_n: int,
        operator: str,
    ) -> str:
        data = {
            "universe_id": universe_id,
            "as_of": as_of.isoformat(),
            "top_n": top_n,
            "operator": operator,
            "mode": TradingMode.PAPER.value,
        }
        return hashlib.sha256(json.dumps(
            data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
