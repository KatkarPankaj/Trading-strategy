"""Bounded, resumable PAPER-only opportunity discovery ending at strategy selection."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .ai.candidate_assessment import (
    CandidateAssessmentService,
    OpportunityState,
)
from .candidate_research import CandidateResearchService
from .markets import MarketRegistry, UnknownMarket
from .models import AssetClass
from .scanner import MarketScanner, ScanMode

logger = logging.getLogger(__name__)

MAX_AUTONOMOUS_CANDIDATES = 10
MAX_AUTONOMOUS_CONCURRENCY = 4


class AutonomousResearchError(ValueError):
    """The autonomous run cannot safely continue."""


class IdempotencyConflict(AutonomousResearchError):
    """An idempotency key was reused for a different request."""


@dataclass(frozen=True, slots=True)
class AutonomousResearchSettings:
    max_candidates: int = 10
    max_concurrency: int = 4
    allowed_markets: tuple[str, ...] = ()
    allowed_asset_classes: tuple[AssetClass, ...] = ()
    strategy_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.max_candidates, bool) or not isinstance(
                self.max_candidates, int) or not 1 <= self.max_candidates <= MAX_AUTONOMOUS_CANDIDATES:
            raise ValueError("max_candidates must be between 1 and 10")
        if isinstance(self.max_concurrency, bool) or not isinstance(
                self.max_concurrency, int) or not 1 <= self.max_concurrency <= MAX_AUTONOMOUS_CONCURRENCY:
            raise ValueError("max_concurrency must be between 1 and 4")
        if not self.allowed_markets or any(
                not isinstance(item, str) or not item.strip()
                for item in self.allowed_markets) or len(
                    set(self.allowed_markets)) != len(self.allowed_markets):
            raise ValueError("allowed_markets must contain unique market codes")
        if not self.allowed_asset_classes or any(
                not isinstance(item, AssetClass) for item in self.allowed_asset_classes) \
                or len(set(self.allowed_asset_classes)) != len(self.allowed_asset_classes):
            raise ValueError("allowed_asset_classes must contain unique asset classes")
        if not self.strategy_allowlist or any(
                not isinstance(item, str) or not item.strip()
                for item in self.strategy_allowlist) or len(
                    set(self.strategy_allowlist)) != len(self.strategy_allowlist):
            raise ValueError("strategy_allowlist must contain registered strategy names")


def parse_autonomous_research_settings(
    raw: str | None,
    *,
    default_markets: tuple[str, ...],
    default_asset_classes: tuple[AssetClass, ...],
    default_strategies: tuple[str, ...],
) -> AutonomousResearchSettings:
    if raw is None or not raw.strip():
        return AutonomousResearchSettings(
            allowed_markets=default_markets,
            allowed_asset_classes=default_asset_classes,
            strategy_allowlist=default_strategies,
        )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("AUTONOMOUS_RESEARCH_SETTINGS must be valid JSON") from exc
    allowed = {
        "max_candidates", "max_concurrency", "allowed_markets",
        "allowed_asset_classes", "strategy_allowlist",
    }
    if not isinstance(values, dict) or set(values).difference(allowed):
        raise ValueError("AUTONOMOUS_RESEARCH_SETTINGS contains unsupported fields")
    converted = dict(values)
    for key in ("allowed_markets", "allowed_asset_classes", "strategy_allowlist"):
        if key in converted and not isinstance(converted[key], list):
            raise ValueError(f"{key} must be a JSON array")
    if "allowed_asset_classes" in converted:
        try:
            converted["allowed_asset_classes"] = tuple(
                AssetClass(item) for item in converted["allowed_asset_classes"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "allowed_asset_classes must contain supported asset classes") from exc
    for key in ("allowed_markets", "allowed_asset_classes", "strategy_allowlist"):
        if key in converted:
            converted[key] = tuple(converted[key])
    converted.setdefault("allowed_markets", default_markets)
    converted.setdefault("allowed_asset_classes", default_asset_classes)
    converted.setdefault("strategy_allowlist", default_strategies)
    return AutonomousResearchSettings(**converted)


class AutonomousResearchService:
    """Compose scanner, persisted research, and AI ranking; never enter execution."""

    def __init__(
        self,
        scanner: MarketScanner,
        candidate_research: CandidateResearchService,
        candidate_assessment: CandidateAssessmentService,
        repository: Any,
        markets: MarketRegistry,
        settings: AutonomousResearchSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        unknown = set(settings.strategy_allowlist).difference(
            candidate_assessment.strategies)
        if unknown:
            raise ValueError(
                f"strategy allowlist contains unregistered strategies: {sorted(unknown)}")
        if settings.max_concurrency > candidate_research.settings.max_concurrency:
            raise ValueError(
                "max_concurrency cannot exceed candidate-research concurrency")
        if settings.max_concurrency > scanner.max_concurrency:
            raise ValueError("max_concurrency cannot exceed scanner concurrency")
        if scanner.repository is None:
            raise ValueError("autonomous research requires a persistent scanner repository")
        self.scanner = scanner
        self.candidate_research = candidate_research
        self.candidate_assessment = candidate_assessment
        self.repository = repository
        self.markets = markets
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.repository.get_run(run_id)
        return None if row is None else self._response(row)

    def run(
        self,
        universe_id: str,
        *,
        idempotency_key: str,
        mode: str = "PAPER",
        as_of: datetime | None = None,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        if mode != "PAPER":
            raise AutonomousResearchError("autonomous opportunity discovery is PAPER-only")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() \
                or len(idempotency_key) > 128 \
                or any(not (ch.isascii() and (ch.isalnum() or ch in "_-"))
                       for ch in idempotency_key):
            raise ValueError("idempotency_key must be a safe identifier of at most 128 characters")
        now = self._now()
        persisted = self.repository.get_by_key(idempotency_key)
        timestamp = (
            datetime.fromisoformat(str(persisted["as_of"]))
            if as_of is None and persisted is not None
            else now if as_of is None else as_of
        )
        self._aware(timestamp, "as_of")
        if timestamp > now:
            raise ValueError("as_of cannot be in the future")
        timestamp = timestamp.astimezone(timezone.utc)
        count = self.settings.max_candidates if top_n is None else top_n
        if isinstance(count, bool) or not isinstance(count, int) \
                or not 1 <= count <= self.settings.max_candidates:
            raise ValueError(
                f"top_n must be between 1 and {self.settings.max_candidates}")

        definition, universe_instruments = self.scanner.get_universe(universe_id)
        requested_markets = {item.upper() for item in self.settings.allowed_markets}
        universe_markets = {item.upper() for item in definition.markets}
        active_markets = requested_markets.intersection(universe_markets)
        if not active_markets:
            raise AutonomousResearchError(
                "universe does not contain a configured allowed market")
        definition_asset_classes = set(definition.asset_classes)
        active_asset_classes = tuple(
            item for item in self.settings.allowed_asset_classes
            if item in definition_asset_classes)
        if not active_asset_classes:
            raise AutonomousResearchError(
                "universe does not contain a configured allowed asset class")
        scoped_instrument_ids = sorted(
            (
                instrument.instrument_id,
                instrument.exchange,
                instrument.market,
                instrument.asset_class.value,
                instrument.currency,
                instrument.timezone,
                instrument.tick_size,
                instrument.lot_size,
                instrument.trading_status.value,
                instrument.active,
                instrument.tradable,
            )
            for instrument in universe_instruments
            if instrument.market.upper() in active_markets
            and instrument.asset_class in active_asset_classes)
        instrument_scope_fingerprint = hashlib.sha256(
            json.dumps(scoped_instrument_ids, separators=(",", ":")).encode()
        ).hexdigest()
        strategy_catalog = [
            {
                "name": name,
                "implementation": type(
                    self.candidate_assessment.strategies[name]).__name__,
                "version": str(getattr(
                    self.candidate_assessment.strategies[name],
                    "version", "unspecified"))[:128],
            }
            for name in self.settings.strategy_allowlist
        ]
        for code in active_markets:
            try:
                market = self.markets.get(code)
            except UnknownMarket as exc:
                raise AutonomousResearchError(
                    f"market calendar is unavailable for {code}") from exc
            local_date = timestamp.astimezone(ZoneInfo(market.timezone)).date()
            if not market.is_covered(local_date):
                raise AutonomousResearchError(
                    f"market calendar does not cover {code} at as_of")
            if not market.calendar.is_trading_day(local_date):
                raise AutonomousResearchError(
                    f"as_of falls on a non-trading day for {code}")

        request = {
            "universe_id": universe_id,
            "as_of": timestamp.isoformat(),
            "mode": mode,
            "top_n": count,
            "markets": sorted(active_markets),
            "asset_classes": sorted(item.value for item in active_asset_classes),
            "strategy_allowlist": list(self.settings.strategy_allowlist),
            "max_concurrency": self.settings.max_concurrency,
            "scanner_fingerprint": self.scanner.configuration_fingerprint,
            "research_fingerprint": self.candidate_research.configuration_fingerprint,
            "assessment_fingerprint": (
                self.candidate_assessment.configuration_fingerprint),
            "strategy_catalog": strategy_catalog,
            "instrument_scope_fingerprint": instrument_scope_fingerprint,
        }
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if persisted is not None:
            if persisted["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "idempotency_key was already used for a different request")
            if persisted["status"] == "COMPLETE":
                return self._response(persisted)
            return self._resume(persisted, request)

        run_id = uuid5(NAMESPACE_URL, f"stockmarket:autonomous:{idempotency_key}").hex
        self.repository.create_run(
            run_id=run_id, idempotency_key=idempotency_key, request_hash=request_hash,
            as_of=timestamp, now=now, payload={"request": request})
        persisted = self.repository.get_run(run_id)
        if persisted is None:
            raise RuntimeError("autonomous research run was not persisted")
        return self._resume(persisted, request)

    def _resume(self, run: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(run["run_id"])
        scan_id = f"auto-{run_id[:28]}"
        research_id = f"auto-{run_id[:28]}"
        timestamp = datetime.fromisoformat(str(request["as_of"]))
        payload = dict(run["payload"])
        try:
            scan_repository = self.scanner.repository
            scan = scan_repository.get(scan_id) if scan_repository else None
            if scan is not None and scan["status"] == "FAILED":
                raise AutonomousResearchError(
                    "persisted research scan failed and cannot be resumed")
            if scan is None:
                self._update(run_id, "RUNNING", "SCANNING", payload)
                result = self.scanner.scan(
                    str(request["universe_id"]), ScanMode.RESEARCH,
                    top_n=int(request["top_n"]), as_of=timestamp, scan_id=scan_id,
                    allowed_markets=tuple(request["markets"]),
                    allowed_asset_classes=tuple(
                        AssetClass(item) for item in request["asset_classes"]),
                    max_concurrency=self.settings.max_concurrency,
                )
                if result.status.value == "FAILED":
                    raise AutonomousResearchError("research scanner failed")
                scan_status = result.status.value
            else:
                scan_status = str(scan["status"])
            payload["scan_id"] = scan_id
            payload["scan_status"] = scan_status
            self._update(run_id, "RUNNING", "SCAN_COMPLETE", payload)

            selected_candidates = self.scanner.repository.candidates(
                scan_id, selected_only=True, limit=int(request["top_n"]))
            if not selected_candidates:
                payload["candidates"] = []
                payload["completion_reason"] = "NO_ELIGIBLE_CANDIDATES"
                return self._update(
                    run_id,
                    "PARTIAL" if scan_status == "PARTIAL" else "COMPLETE",
                    "STRATEGY_SELECTED",
                    payload,
                )

            research_run = self.candidate_research.research_runs.get_run(research_id)
            if research_run is None:
                self._update(run_id, "RUNNING", "RESEARCHING", payload)
                research, snapshots = self.candidate_research.run_scan(
                    scan_id, as_of=timestamp, limit=int(request["top_n"]),
                    run_id=research_id, max_concurrency=self.settings.max_concurrency,
                )
            else:
                saved_fingerprint = research_run["payload"].get(
                    "configuration_fingerprint")
                if saved_fingerprint != self.candidate_research.configuration_fingerprint:
                    raise AutonomousResearchError(
                        "candidate-research configuration changed since the run was persisted")
                research = research_run["payload"]
                snapshots = tuple()
            if isinstance(research, Mapping):
                snapshot_rows = self.candidate_research.research_runs.snapshots(research_id)
                snapshot_ids = [row["snapshot_id"] for row in snapshot_rows]
                research_failures = tuple(
                    research.get("failure_summary", ()))
            else:
                snapshot_ids = [item.snapshot_id for item in snapshots]
                research_failures = research.failure_summary
            payload["research_run_id"] = research_id
            payload["snapshot_ids"] = snapshot_ids
            payload["research_failure_summary"] = list(research_failures)
            self._update(run_id, "RUNNING", "ASSESSING", payload)

            candidate_outputs = []
            for snapshot_id in snapshot_ids:
                snapshot_row = self.candidate_research.research_runs.get_snapshot(snapshot_id)
                if snapshot_row is None:
                    raise RuntimeError(f"persisted snapshot {snapshot_id!r} is missing")
                instrument_id = str(snapshot_row["instrument_id"])
                existing = next(
                    (item for item in self.repository.candidates(run_id)
                     if item["instrument_id"] == instrument_id
                     and item["stage"] in ("STRATEGY_SELECTED", "REJECTED")),
                    None,
                )
                if existing is not None:
                    candidate_outputs.append(existing["payload"])
                    continue
                try:
                    stored_opportunity = (
                        self.candidate_assessment.repository.opportunity_for_snapshot(
                            snapshot_id))
                    if stored_opportunity is not None and self._assessment_matches_request(
                            stored_opportunity, request):
                        opportunity = stored_opportunity
                    else:
                        result = self.candidate_assessment.assess(
                            snapshot_id,
                            strategy_allowlist=tuple(request["strategy_allowlist"]),
                        )
                        opportunity = json.loads(json.dumps(
                            result, default=self._json_default))
                    selected = (
                        opportunity.get("state") == OpportunityState.STRATEGY_SELECTED.value
                        and opportunity.get("assessment", {}).get("recommended_strategy")
                        in request["strategy_allowlist"]
                    )
                    stage = "STRATEGY_SELECTED" if selected else "REJECTED"
                    output = {
                        "instrument_id": instrument_id,
                        "snapshot_id": snapshot_id,
                        "stage": stage,
                        "opportunity": opportunity,
                        "execution": "NOT_SUBMITTED",
                        "risk_status": "NOT_EVALUATED",
                    }
                    self.repository.save_candidate(
                        run_id, instrument_id, stage=stage, now=self._now(),
                        snapshot_id=snapshot_id,
                        opportunity_id=opportunity.get("opportunity_id"),
                        payload=output,
                    )
                except Exception as exc:
                    logger.exception(
                        "autonomous candidate assessment failed",
                        extra={"run_id": run_id, "snapshot_id": snapshot_id})
                    output = {
                        "instrument_id": instrument_id,
                        "snapshot_id": snapshot_id,
                        "stage": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                        "execution": "NOT_SUBMITTED",
                        "risk_status": "NOT_EVALUATED",
                    }
                    self.repository.save_candidate(
                        run_id, instrument_id, stage="FAILED", now=self._now(),
                        snapshot_id=snapshot_id, error=output["error"], payload=output,
                    )
                candidate_outputs.append(output)

            snapshot_instruments = {
                str(row["instrument_id"])
                for row in self.candidate_research.research_runs.snapshots(research_id)
            }
            for candidate in selected_candidates:
                instrument_id = str(candidate["instrument_id"])
                if instrument_id in snapshot_instruments:
                    continue
                reason = next(
                    (item.partition(":")[2] for item in research_failures
                     if item.startswith(f"{instrument_id}:")),
                    "RESEARCH_SNAPSHOT_NOT_CREATED",
                )
                output = {
                    "instrument_id": instrument_id,
                    "snapshot_id": None,
                    "stage": "FAILED",
                    "error": reason,
                    "execution": "NOT_SUBMITTED",
                    "risk_status": "NOT_EVALUATED",
                }
                self.repository.save_candidate(
                    run_id, instrument_id, stage="FAILED", now=self._now(),
                    error=reason, payload=output,
                )
                candidate_outputs.append(output)

            payload["candidates"] = candidate_outputs
            status = "PARTIAL" if any(
                item.get("stage") == "FAILED" for item in candidate_outputs) else "COMPLETE"
            return self._update(run_id, status, "STRATEGY_SELECTED", payload)
        except Exception as exc:
            logger.exception(
                "autonomous opportunity run failed",
                extra={"run_id": run_id, "stage": run.get("stage")})
            payload["failure"] = f"{type(exc).__name__}: {exc}"
            self._update(run_id, "INTERRUPTED", "INTERRUPTED", payload)
            raise

    def _assessment_matches_request(
        self, opportunity: Mapping[str, Any], request: Mapping[str, Any],
    ) -> bool:
        assessment = opportunity.get("assessment", {})
        context = assessment.get("input_context", {})
        return (
            context.get("as_of") == request["as_of"]
            and context.get("registered_strategies") == request["strategy_catalog"]
            and context.get("ai_model_version")
            == self.candidate_assessment.model_version
            and context.get("ai_provider")
            == self.candidate_assessment.analyst.provider_name
        )

    @staticmethod
    def _json_default(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "__dataclass_fields__"):
            return {
                name: getattr(value, name)
                for name in value.__dataclass_fields__
            }
        raise TypeError(f"cannot serialize {type(value).__name__}")

    def _update(
        self, run_id: str, status: str, stage: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.repository.update_run(
            run_id, status=status, stage=stage, now=self._now(), payload=payload)
        persisted = self.repository.get_run(run_id)
        if persisted is None:
            raise RuntimeError("autonomous research run disappeared after checkpoint")
        return self._response(persisted)

    def _response(self, run: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run["run_id"],
            "idempotency_key": run["idempotency_key"],
            "mode": "PAPER",
            "research_only": True,
            "status": run["status"],
            "stage": run["stage"],
            "as_of": run["as_of"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "payload": run["payload"],
            "candidates": [
                item["payload"]
                for item in self.repository.candidates(str(run["run_id"]))
            ],
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
        }

    def _now(self) -> datetime:
        value = self.clock()
        self._aware(value, "clock")
        return value

    @staticmethod
    def _aware(value: datetime, name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
