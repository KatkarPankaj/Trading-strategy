"""Bounded, durable orchestration of the PAPER research-to-order workflow."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from .executors import TradingMode


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
            payload["position_management"] = self.positions.manage()
            self._assert_lease(lease_lost, lease_error)

            if self.gate.halted:
                payload["halt_reason"] = self.gate.blocked_reason()
                return self._finish(
                    run_id, payload, "PARTIAL", "HALTED", owner_token)

            research_result = self.research.run(
                request["universe_id"],
                idempotency_key="paper-" + hashlib.sha256(
                    request["idempotency_key"].encode()).hexdigest(),
                mode=TradingMode.PAPER.value,
                as_of=datetime.fromisoformat(request["as_of"]),
                top_n=request["top_n"],
            )
            research_run_id = str(research_result["run_id"])
            payload["research_run_id"] = research_run_id
            self.repository.update_run(
                run_id, status="RUNNING", stage="RESEARCH_COMPLETE",
                payload=payload, owner_token=owner_token)
            self._assert_lease(lease_lost, lease_error)
            selected = [
                candidate for candidate in research_result.get("candidates", [])
                if candidate.get("stage") == "STRATEGY_SELECTED"
            ][:request["top_n"]]
            selected_ids = {str(item["instrument_id"]) for item in selected}
            for candidate in research_result.get("candidates", []):
                candidate_id = str(candidate.get("instrument_id", ""))
                if not candidate_id:
                    continue
                if candidate_id not in selected_ids:
                    self.repository.save_candidate(
                        run_id, candidate_id, status="RESEARCH_REJECTED",
                        payload={
                            "instrument_id": candidate_id,
                            "research_stage": candidate.get("stage"),
                            "research_error": candidate.get("error"),
                        })
            self._checkpoint_payload(
                run_id, payload, "PROCESSING_CANDIDATES", owner_token)

            failures = sum(
                1 for candidate in research_result.get("candidates", [])
                if candidate.get("stage") == "FAILED")
            completed = 0
            for candidate in selected:
                self._assert_lease(lease_lost, lease_error)
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
                        research_run_id=research_run_id,
                        candidate_id=candidate_id,
                        operator=request["operator"],
                        as_of=datetime.fromisoformat(request["as_of"]),
                        lease_lost=lease_lost,
                        lease_error=lease_error,
                    )
                    self.repository.save_candidate(
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
                    self.repository.save_candidate(
                        run_id, candidate_id, status="FAILED", payload=outcome)
                    self._event(
                        run_id, candidate_id, "CANDIDATE_FAILED", outcome)

            payload["position_management"] = self.positions.manage()
            self._assert_lease(lease_lost, lease_error)
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
        research_run_id: str,
        candidate_id: str,
        operator: str,
        as_of: datetime,
        lease_lost: threading.Event,
        lease_error: list[str],
    ) -> dict[str, Any]:
        self.repository.save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={"instrument_id": candidate_id, "stage": "SIGNAL_GENERATION"})
        signal_result = self.signals.generate(
            research_run_id, candidate_id, evaluation_as_of=as_of)
        self._assert_lease(lease_lost, lease_error)
        outcome: dict[str, Any] = {
            "instrument_id": candidate_id,
            "signal": signal_result,
            "execution": "PAPER",
        }
        if signal_result.get("status") != "SIGNAL_GENERATED":
            outcome["status"] = "FAILED"
            outcome["error"] = signal_result.get(
                "reason", "signal generation rejected")
            return outcome

        self.repository.save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={**outcome, "stage": "RISK_EVALUATION"})
        risk_result = self.proposals.evaluate(
            research_run_id, candidate_id, evaluation_as_of=as_of)
        self._assert_lease(lease_lost, lease_error)
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
        self.repository.save_candidate(
            run_id, candidate_id, status="IN_PROGRESS",
            payload={**outcome, "stage": "ORDER_SUBMISSION"})
        execution_result = self.execution.submit(
            proposal_id,
            operator=operator,
            sizing_mode="AUTOMATIC_SIZING",
            quantity=None,
        )
        self._assert_lease(lease_lost, lease_error)
        order = execution_result["order"]
        outcome["order"] = {
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
            "error": order.error,
        }
        outcome["status"] = (
            "COMPLETE" if order.status.value in {
                "ACCEPTED", "PARTIALLY_FILLED", "FILLED",
            } else "ORDER_REJECTED")
        return outcome

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
                ):
                    lease_error.append("paper cycle execution lease was lost")
                    lease_lost.set()
                    return
            except Exception as exc:
                lease_error.append(
                    f"paper cycle lease renewal failed: {type(exc).__name__}: {exc}")
                lease_lost.set()
                return

    @staticmethod
    def _assert_lease(
        lease_lost: threading.Event,
        lease_error: list[str],
    ) -> None:
        if lease_lost.is_set():
            raise AutonomousPaperCycleError(
                "; ".join(lease_error) or "paper cycle execution lease was lost")

    def _now(self) -> datetime:
        return self._aware(self.clock(), "clock").astimezone(timezone.utc)

    def _lease_until(self) -> datetime:
        return datetime.now(timezone.utc) + self.lease_duration

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
