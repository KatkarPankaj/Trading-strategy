"""Timestamp-bound AI assessment and deterministic ranking of persisted snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .analyst import AIAnalyst
from .schemas import CandidateAssessmentSchema
from ..markets import MarketRegistry
from ..models import Instrument
from ..strategies.base import Strategy

PROMPT_VERSION = "candidate-assessment-v1"
SCHEMA_VERSION = "candidate-assessment-v1"
_COMPONENT_WEIGHTS = {
    "technical": 0.25,
    "regime": 0.25,
    "news": 0.15,
    "fundamental": 0.15,
    "sector": 0.10,
    "macro": 0.05,
    "sentiment": 0.05,
}


class AssessmentStatus(str, Enum):
    COMPLETE = "COMPLETE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_INVALID = "AI_INVALID"
    NO_VALID_STRATEGY = "NO_VALID_STRATEGY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class OpportunityState(str, Enum):
    STRATEGY_SELECTED = "STRATEGY_SELECTED"
    REJECTED = "REJECTED"


class DirectionalBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class AIResearchContext:
    snapshot_id: str
    instrument_id: str
    symbol: str
    exchange: str
    market: str
    asset_class: str
    currency: str
    timezone: str
    market_session: Mapping[str, Any]
    as_of: datetime
    snapshot_created_at: datetime
    snapshot_status: str
    scanner_rank: int
    scanner_score: float
    components: tuple[Mapping[str, Any], ...]
    technical_regime_scores: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...]
    evidence_completeness: float
    data_quality: float
    ai_provider: str
    ai_model_version: str
    prompt_version: str
    schema_version: str
    registered_strategies: tuple[Mapping[str, str], ...]
    missing_evidence: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "market": self.market,
            "asset_class": self.asset_class,
            "currency": self.currency,
            "timezone": self.timezone,
            "market_session": dict(self.market_session),
            "as_of": self.as_of.isoformat(),
            "snapshot_created_at": self.snapshot_created_at.isoformat(),
            "snapshot_status": self.snapshot_status,
            "scanner_rank": self.scanner_rank,
            "scanner_score": self.scanner_score,
            "components": [dict(item) for item in self.components],
            "technical_regime_scores": dict(self.technical_regime_scores),
            "evidence": [dict(item) for item in self.evidence],
            "evidence_completeness": self.evidence_completeness,
            "data_quality": self.data_quality,
            "ai_provider": self.ai_provider,
            "ai_model_version": self.ai_model_version,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "registered_strategies": [
                dict(item) for item in self.registered_strategies],
            "missing_evidence": list(self.missing_evidence),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class AIResearchAssessment:
    assessment_id: str
    snapshot_id: str
    instrument_id: str
    status: AssessmentStatus
    directional_bias: DirectionalBias
    confidence: float | None
    opportunity_score: float | None
    risk_flags: tuple[str, ...]
    key_evidence: tuple[str, ...]
    invalidating_conditions: tuple[str, ...]
    explanation: str
    recommended_strategy: str | None
    strategy_reason: str | None
    provider: str
    model_version: str
    prompt_version: str
    schema_version: str
    assessed_at: datetime
    input_context: AIResearchContext
    error: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    strategy_selection_prompt_hash: str | None = None
    strategy_selection_response_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OpportunityRanking:
    score: float
    components: Mapping[str, float]
    risk_penalty: float
    explanation: str


@dataclass(frozen=True, slots=True)
class AIResearchOpportunity:
    opportunity_id: str
    assessment: AIResearchAssessment
    state: OpportunityState
    ranking: OpportunityRanking | None
    lifecycle: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OpportunityRankingConfig:
    completeness_weight: float = 0.20
    quality_weight: float = 0.15
    technical_weight: float = 0.20
    regime_weight: float = 0.15
    ai_score_weight: float = 0.15
    confidence_weight: float = 0.05
    strategy_weight: float = 0.10
    risk_flag_penalty: float = 5.0
    maximum_risk_penalty: float = 25.0

    def __post_init__(self) -> None:
        weights = (
            self.completeness_weight, self.quality_weight, self.technical_weight,
            self.regime_weight, self.ai_score_weight, self.confidence_weight,
            self.strategy_weight,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not isfinite(value) or value < 0
               for value in weights):
            raise ValueError("ranking weights must be finite and non-negative")
        if abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("ranking weights must sum to 1")
        if isinstance(self.risk_flag_penalty, bool) \
                or not isinstance(self.risk_flag_penalty, (int, float)) \
                or not isfinite(self.risk_flag_penalty) or self.risk_flag_penalty < 0 \
                or isinstance(self.maximum_risk_penalty, bool) \
                or not isinstance(self.maximum_risk_penalty, (int, float)) \
                or not isfinite(self.maximum_risk_penalty) \
                or self.maximum_risk_penalty < 0:
            raise ValueError("risk penalties must be finite and non-negative")


class CandidateAssessmentError(ValueError):
    """A persisted snapshot cannot safely be assessed."""


class CandidateAssessmentRepository(Protocol):
    def get_snapshot(self, snapshot_id: str) -> Mapping[str, Any] | None: ...

    def save_opportunity(self, opportunity: AIResearchOpportunity) -> None: ...

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None: ...

    def opportunity_for_snapshot(self, snapshot_id: str) -> dict[str, Any] | None: ...

    def list_opportunities(
        self, *, limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]: ...


class CandidateAssessmentService:
    def __init__(
        self,
        analyst: AIAnalyst,
        repository: CandidateAssessmentRepository,
        instruments: Mapping[str, Instrument],
        markets: MarketRegistry,
        strategies: Mapping[str, Strategy],
        *,
        model_version: str = "unspecified",
        ranking: OpportunityRankingConfig = OpportunityRankingConfig(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(model_version, str) or not model_version.strip() \
                or len(model_version.strip()) > 128:
            raise ValueError("model_version must be a non-empty name of at most 128 characters")
        if any(not isinstance(name, str) or not name.strip()
               or name != name.strip() or len(name) > 80 for name in strategies):
            raise ValueError("registered strategy names must be trimmed and at most 80 characters")
        if len(strategies) > 20:
            raise ValueError("at most 20 registered strategies can be assessed")
        self.analyst = analyst
        self.repository = repository
        self.instruments = instruments
        self.markets = markets
        self.strategies = strategies
        self.model_version = model_version.strip()
        self.ranking_config = ranking
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def configuration_fingerprint(self) -> str:
        catalog = [
            {
                "name": name,
                "implementation": type(strategy).__name__,
                "version": str(getattr(strategy, "version", "unspecified"))[:128],
            }
            for name, strategy in self.strategies.items()
        ]
        configuration = {
            "provider": self.analyst.provider_name,
            "model_version": self.model_version,
            "max_input_chars": self.analyst.max_input_chars,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "ranking": self.ranking_config,
            "catalog": catalog,
        }
        text = json.dumps(configuration, sort_keys=True, separators=(",", ":"),
                          default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def assess(
        self,
        snapshot_id: str,
        *,
        strategy_allowlist: tuple[str, ...] | None = None,
    ) -> AIResearchOpportunity:
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string")
        strategies = self.strategies
        if strategy_allowlist is not None:
            if not isinstance(strategy_allowlist, tuple) or any(
                not isinstance(name, str) or name not in self.strategies
                for name in strategy_allowlist
            ) or len(set(strategy_allowlist)) != len(strategy_allowlist):
                raise ValueError(
                    "strategy_allowlist must contain unique registered strategy names")
            strategies = {
                name: self.strategies[name] for name in strategy_allowlist
            }
        row = self.repository.get_snapshot(snapshot_id)
        if row is None:
            raise KeyError(f"unknown research snapshot {snapshot_id!r}")
        context = self._context(row, strategies)
        request_text = json.dumps(
            context.as_mapping(), sort_keys=True, separators=(",", ":"))
        if len(request_text) > self.analyst.max_input_chars:
            assessed_at = self._now()
            assessment = self._failed_assessment(
                context, AssessmentStatus.AI_INVALID, self.analyst.provider_name, assessed_at,
                "snapshot context exceeds the configured AI input limit")
            return self._persist(self._opportunity(
                assessment, OpportunityState.REJECTED, None, assessed_at))
        assessment_result = self.analyst.analyze(
            "candidate_assessment",
            request_text,
            context={
                "snapshot_id": context.snapshot_id,
                "instrument_id": context.instrument_id,
                "as_of": context.as_of.isoformat(),
                "prompt_version": PROMPT_VERSION,
            },
        )
        assessed_at = self._now()
        if not assessment_result.ok:
            status = (
                AssessmentStatus.AI_INVALID
                if assessment_result.error
                and assessment_result.error.startswith("SCHEMA_VALIDATION_FAILED")
                else AssessmentStatus.AI_UNAVAILABLE
            )
            assessment = self._failed_assessment(
                context, status, assessment_result.provider, assessed_at,
                assessment_result.error or status.value,
                prompt_hash=assessment_result.prompt_hash,
                response_hash=assessment_result.response_hash)
            return self._persist(self._opportunity(
                assessment, OpportunityState.REJECTED, None, assessed_at))
        if not self._result_time_is_valid(assessment_result.created_at, assessed_at):
            assessment = self._failed_assessment(
                context, AssessmentStatus.AI_INVALID, assessment_result.provider,
                assessed_at, "AI assessment timestamp is invalid",
                prompt_hash=assessment_result.prompt_hash,
                response_hash=assessment_result.response_hash)
            return self._persist(self._opportunity(
                assessment, OpportunityState.REJECTED, None, assessed_at))
        output = assessment_result.output
        if not isinstance(output, CandidateAssessmentSchema) \
                or output.snapshot_id != context.snapshot_id \
                or output.instrument_id != context.instrument_id \
                or not set(output.key_evidence).issubset(
                    {str(item["evidence_id"]) for item in context.evidence}):
            assessment = self._failed_assessment(
                context, AssessmentStatus.AI_INVALID, assessment_result.provider,
                assessed_at, "AI assessment identity or evidence citations failed validation",
                prompt_hash=assessment_result.prompt_hash,
                response_hash=assessment_result.response_hash)
            return self._persist(self._opportunity(
                assessment, OpportunityState.REJECTED, None, assessed_at))

        bias = DirectionalBias(output.directional_bias)
        missing_critical = self._has_missing_critical_evidence(context)
        if missing_critical and bias is not DirectionalBias.INSUFFICIENT_EVIDENCE:
            assessment = self._failed_assessment(
                context, AssessmentStatus.AI_INVALID, assessment_result.provider,
                assessed_at, "AI assessment contradicted missing critical evidence",
                prompt_hash=assessment_result.prompt_hash,
                response_hash=assessment_result.response_hash)
            return self._persist(self._opportunity(
                assessment, OpportunityState.REJECTED, None, assessed_at))
        selection_error = None
        selection_result = None
        selection = None
        if strategies and bias is not DirectionalBias.INSUFFICIENT_EVIDENCE:
            selection_context = {
                **context.as_mapping(),
                "registered_strategies": [
                    dict(item) for item in context.registered_strategies],
                "ai_assessment": {
                    "directional_bias": bias.value,
                    "confidence": output.confidence,
                    "opportunity_score": output.opportunity_score,
                    "risk_flags": output.risk_flags,
                    "key_evidence": output.key_evidence,
                    "invalidating_conditions": output.invalidating_conditions,
                    "explanation": output.explanation,
                },
            }
            selection_request = json.dumps({
                "available_strategies": tuple(strategies),
                "research": selection_context,
            }, sort_keys=True, default=str)
            if len(selection_request) > self.analyst.max_input_chars:
                selection_error = (
                    "strategy-selection context exceeds the configured AI input limit")
            else:
                selection_result, selection = self.analyst.select_strategies(
                    instrument_id=context.instrument_id,
                    as_of=context.as_of,
                    available_strategies=tuple(strategies),
                    research_context=selection_context,
                )
                if selection_result.ok and not self._result_time_is_valid(
                        selection_result.created_at, assessed_at):
                    selection = None
                    selection_error = "AI strategy-selection timestamp is invalid"
        strategy = selection.ranked_strategies[0] if selection else None
        if strategy is None:
            if selection_error is not None:
                status = AssessmentStatus.AI_INVALID
            elif bias is DirectionalBias.INSUFFICIENT_EVIDENCE:
                status = AssessmentStatus.INSUFFICIENT_EVIDENCE
            elif selection_result is not None and not selection_result.ok \
                    and selection_result.error:
                if selection_result.error.startswith("SCHEMA_VALIDATION_FAILED"):
                    status = AssessmentStatus.AI_INVALID
                elif selection_result.error.startswith("AI_"):
                    status = AssessmentStatus.AI_UNAVAILABLE
                else:
                    status = AssessmentStatus.NO_VALID_STRATEGY
            else:
                status = AssessmentStatus.NO_VALID_STRATEGY
            state = OpportunityState.REJECTED
            error = selection_error or (
                selection_result.error if selection_result is not None
                else "insufficient evidence or no registered strategies")
        else:
            status = AssessmentStatus.COMPLETE
            state = OpportunityState.STRATEGY_SELECTED
            error = None
        assessment = AIResearchAssessment(
            assessment_id=str(uuid4()),
            snapshot_id=context.snapshot_id,
            instrument_id=context.instrument_id,
            status=status,
            directional_bias=bias,
            confidence=output.confidence,
            opportunity_score=output.opportunity_score,
            risk_flags=tuple(output.risk_flags),
            key_evidence=tuple(output.key_evidence),
            invalidating_conditions=tuple(output.invalidating_conditions),
            explanation=output.explanation,
            recommended_strategy=strategy.strategy if strategy else None,
            strategy_reason=strategy.rationale if strategy else None,
            provider=assessment_result.provider,
            model_version=self.model_version,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            assessed_at=assessed_at,
            input_context=context,
            prompt_hash=assessment_result.prompt_hash,
            response_hash=assessment_result.response_hash,
            strategy_selection_prompt_hash=(
                selection_result.prompt_hash if selection_result is not None else None),
            strategy_selection_response_hash=(
                selection_result.response_hash if selection_result is not None else None),
            error=error,
        )
        ranking = self.rank(
            assessment, context, strategy_names=frozenset(strategies)
        ) if assessment.opportunity_score is not None else None
        return self._persist(self._opportunity(assessment, state, ranking, assessed_at))

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        return self.repository.get_assessment(assessment_id)

    def opportunities(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.repository.list_opportunities(limit=limit, offset=offset)

    def rank(
        self,
        assessment: AIResearchAssessment,
        context: AIResearchContext | None = None,
        *,
        strategy_names: frozenset[str] | None = None,
    ) -> OpportunityRanking:
        context = context or assessment.input_context
        components = {str(item["name"]): str(item["status"])
                      for item in context.components}
        completeness = sum(
            weight * (1.0 if components.get(name) == "AVAILABLE" else 0.0)
            for name, weight in _COMPONENT_WEIGHTS.items())
        valid = sum(1 for item in context.evidence if item.get("quality") == "VALID")
        quality = valid / len(context.evidence) if context.evidence else 0.0
        technical = self._directional_alignment(
            assessment.directional_bias,
            self._score(context.technical_regime_scores, "momentum"),
            self._score(context.technical_regime_scores, "volume"),
        )
        regime = self._directional_alignment(
            assessment.directional_bias,
            self._score(context.technical_regime_scores, "regime"),
        )
        ai_score = (assessment.opportunity_score or 0.0) / 100.0
        confidence = assessment.confidence or 0.0
        catalog = strategy_names if strategy_names is not None else frozenset(self.strategies)
        strategy = 1.0 if assessment.recommended_strategy in catalog else 0.0
        parts = {
            "evidence_completeness": completeness,
            "data_quality": quality,
            "technical_alignment": technical,
            "regime_compatibility": regime,
            "ai_opportunity_score": ai_score,
            "ai_confidence": confidence,
            "registered_strategy": strategy,
        }
        cfg = self.ranking_config
        score = 100.0 * (
            cfg.completeness_weight * completeness
            + cfg.quality_weight * quality
            + cfg.technical_weight * technical
            + cfg.regime_weight * regime
            + cfg.ai_score_weight * ai_score
            + cfg.confidence_weight * confidence
            + cfg.strategy_weight * strategy
        )
        penalty = min(
            cfg.maximum_risk_penalty,
            cfg.risk_flag_penalty * len(assessment.risk_flags))
        score = round(max(0.0, min(100.0, score - penalty)), 4)
        explanation = (
            "Advisory ranking (0-100; not a probability): "
            + ", ".join(f"{name}={value:.3f}" for name, value in parts.items())
            + f"; risk_penalty={penalty:.2f}; final={score:.4f}."
        )
        return OpportunityRanking(score, parts, penalty, explanation)

    def _context(
        self, row: Mapping[str, Any], strategies: Mapping[str, Strategy],
    ) -> AIResearchContext:
        payload = row["payload"]
        if not isinstance(payload, Mapping):
            raise CandidateAssessmentError("persisted snapshot payload is invalid")
        instrument_id = str(row["instrument_id"])
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            raise CandidateAssessmentError(
                f"snapshot instrument {instrument_id!r} is not registered")
        if instrument.instrument_id != instrument_id:
            raise CandidateAssessmentError("instrument registry identity mismatch")
        if str(payload.get("instrument_id")) != instrument_id \
                or str(payload.get("snapshot_id")) != str(row["snapshot_id"]):
            raise CandidateAssessmentError("persisted snapshot identity mismatch")
        as_of = self._parse_time(payload.get("as_of"), "snapshot as_of")
        if row.get("as_of") is not None \
                and self._parse_time(row["as_of"], "stored snapshot as_of") != as_of:
            raise CandidateAssessmentError("persisted snapshot as_of mismatch")
        if payload.get("status") not in ("COMPLETE", "PARTIAL"):
            raise CandidateAssessmentError("snapshot is not eligible for AI assessment")
        created = self._parse_time(payload.get("created_at"), "snapshot created_at")
        if as_of > self._now():
            raise CandidateAssessmentError("snapshot as_of is in the future")
        if created > self._now():
            raise CandidateAssessmentError("snapshot creation time is in the future")
        if payload.get("symbol") != instrument.symbol \
                or payload.get("market") != instrument.market:
            raise CandidateAssessmentError("persisted snapshot instrument metadata mismatch")
        market = self.markets.get(instrument.market)
        local_as_of = as_of.astimezone(ZoneInfo(market.calendar.timezone))
        session = (
            market.phase(as_of).value if market.is_covered(local_as_of.date())
            else "UNKNOWN_CALENDAR_COVERAGE"
        )
        session_data = {
            "phase": session,
            "market": market.code,
            "timezone": market.timezone,
            "local_date": local_as_of.date().isoformat(),
            "calendar_covered": market.is_covered(local_as_of.date()),
            "regular_open": market.calendar.open_time.isoformat(),
            "regular_close": market.calendar.close_time_on(
                local_as_of.date()).isoformat(),
            "pre_market_open": (
                market.pre_market_open.isoformat()
                if market.pre_market_open is not None else None),
            "post_market_close": (
                market.post_market_close.isoformat()
                if market.post_market_close is not None else None),
        }
        components: list[Mapping[str, Any]] = []
        missing: list[str] = []
        raw_components = payload.get("components")
        if not isinstance(raw_components, list):
            raise CandidateAssessmentError("invalid persisted component assessments")
        for item in raw_components:
            if not isinstance(item, Mapping):
                raise CandidateAssessmentError("invalid persisted component assessment")
            name = item.get("component")
            status = item.get("status")
            if not isinstance(name, str) or not isinstance(status, str):
                raise CandidateAssessmentError("invalid persisted component assessment")
            reasons = item.get("reasons", [])
            components.append({
                "name": name, "status": status,
                "count": item.get("evidence_count", 0),
                "source": item.get("source"), "reasons": reasons,
            })
            if status != "AVAILABLE":
                missing.append(f"{name}:{status}")
        evidence = []
        raw_evidence = payload.get("evidence")
        if not isinstance(raw_evidence, list):
            raise CandidateAssessmentError("invalid persisted evidence")
        seen_evidence: set[str] = set()
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                raise CandidateAssessmentError("invalid persisted evidence record")
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise CandidateAssessmentError("persisted evidence has no identity")
            if evidence_id in seen_evidence:
                raise CandidateAssessmentError("persisted evidence identities are duplicated")
            seen_evidence.add(evidence_id)
            if item.get("instrument_id") != instrument_id:
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} has a different instrument identity")
            evidence_as_of = self._parse_time(item.get("as_of"), "evidence as_of")
            if evidence_as_of != as_of:
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} has a different as_of timestamp")
            observed = self._parse_time(item["observed_at"], "evidence observed_at") \
                if item.get("observed_at") else None
            retrieved = self._parse_time(item["retrieved_at"], "evidence retrieved_at")
            if observed is not None and observed > as_of:
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} was observed after snapshot as_of")
            if retrieved > created:
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} was retrieved after snapshot creation")
            if observed is not None and retrieved < observed:
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} was retrieved before observation")
            quality = item.get("quality")
            if quality not in ("VALID", "STALE", "INVALID", "UNKNOWN"):
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} has an invalid quality value")
            if not isinstance(item.get("payload", {}), Mapping) \
                    or not isinstance(item.get("provenance", {}), Mapping):
                raise CandidateAssessmentError(
                    f"evidence {evidence_id!r} has invalid payload or provenance")
            age = None if observed is None else max(0.0, (as_of - observed).total_seconds())
            evidence.append({
                "evidence_id": evidence_id,
                "component": item.get("component"),
                "as_of": evidence_as_of.isoformat(),
                "observed_at": observed.isoformat() if observed else None,
                "retrieved_at": retrieved.isoformat(),
                "age_seconds_at_as_of": age,
                "source": item.get("source"),
                "quality": quality,
                "payload": item.get("payload", {}),
                "provenance": item.get("provenance", {}),
            })
        raw_scores = payload.get("scores", [])
        if not isinstance(raw_scores, list):
            raise CandidateAssessmentError("invalid persisted technical/regime scores")
        scores: dict[str, Any] = {}
        for item in raw_scores:
            if not isinstance(item, Mapping) or not isinstance(item.get("component"), str):
                raise CandidateAssessmentError("invalid persisted technical/regime score")
            if item.get("instrument_id") != instrument_id:
                raise CandidateAssessmentError(
                    "persisted technical/regime score has a different instrument identity")
            score_time = self._parse_time(
                item.get("observed_at"), "technical/regime score observed_at")
            if score_time > as_of:
                raise CandidateAssessmentError(
                    "technical/regime score was observed after snapshot as_of")
            raw_score = item.get("score")
            if isinstance(raw_score, bool):
                raise CandidateAssessmentError("technical/regime score is invalid")
            try:
                numeric_score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise CandidateAssessmentError(
                    "technical/regime score is invalid") from exc
            if not isfinite(numeric_score) or not -1.0 <= numeric_score <= 1.0:
                raise CandidateAssessmentError("technical/regime score is invalid")
            scores[item["component"]] = numeric_score
        weighted_complete = sum(
            weight for name, weight in _COMPONENT_WEIGHTS.items()
            if any(c["name"] == name and c["status"] == "AVAILABLE" for c in components))
        quality_count = sum(1 for item in evidence if item["quality"] == "VALID")
        quality = quality_count / len(evidence) if evidence else 0.0
        return AIResearchContext(
            snapshot_id=str(row["snapshot_id"]),
            instrument_id=instrument_id,
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            market=instrument.market,
            asset_class=instrument.asset_class.value,
            currency=instrument.currency,
            timezone=instrument.timezone,
            market_session=session_data,
            as_of=as_of,
            snapshot_created_at=created,
            snapshot_status=str(payload.get("status", row.get("status", "UNKNOWN"))),
            scanner_rank=int(payload.get("scanner_rank", row.get("scanner_rank", 0))),
            scanner_score=float(payload.get("scanner_score", row.get("scanner_score", 0))),
            components=tuple(components),
            technical_regime_scores=scores,
            evidence=tuple(evidence),
            evidence_completeness=round(weighted_complete, 4),
            data_quality=round(quality, 4),
            ai_provider=self.analyst.provider_name,
            ai_model_version=self.model_version,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            registered_strategies=tuple(
                {
                    "name": name,
                    "implementation": type(strategy).__name__,
                    "version": str(getattr(strategy, "version", "unspecified"))[:128],
                }
                for name, strategy in strategies.items()
            ),
            missing_evidence=tuple(missing),
            warnings=tuple(str(x) for x in payload.get("warnings", [])),
        )

    def _has_missing_critical_evidence(self, context: AIResearchContext) -> bool:
        statuses = {str(item["name"]): str(item["status"])
                    for item in context.components}
        evidence_components = {
            str(item["component"]) for item in context.evidence
            if item.get("quality") == "VALID"
        }
        scores = context.technical_regime_scores
        return (
            statuses.get("technical") != "AVAILABLE"
            or statuses.get("regime") != "AVAILABLE"
            or not {"technical", "regime"}.issubset(evidence_components)
            or "momentum" not in scores
            or "regime" not in scores
        )

    def _failed_assessment(
        self, context: AIResearchContext, status: AssessmentStatus, provider: str,
        assessed_at: datetime, error: str, *, prompt_hash: str | None = None,
        response_hash: str | None = None,
    ) -> AIResearchAssessment:
        return AIResearchAssessment(
            assessment_id=str(uuid4()), snapshot_id=context.snapshot_id,
            instrument_id=context.instrument_id, status=status,
            directional_bias=DirectionalBias.INSUFFICIENT_EVIDENCE,
            confidence=None, opportunity_score=None, risk_flags=(),
            key_evidence=(), invalidating_conditions=(),
            explanation="No valid AI assessment was produced; this candidate is rejected.",
            recommended_strategy=None, strategy_reason=None, provider=provider,
            model_version=self.model_version, prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION, assessed_at=assessed_at,
            input_context=context, error=error[:500],
            prompt_hash=prompt_hash, response_hash=response_hash,
        )

    def _opportunity(
        self, assessment: AIResearchAssessment, state: OpportunityState,
        ranking: OpportunityRanking | None, created_at: datetime,
    ) -> AIResearchOpportunity:
        lifecycle = ("DISCOVERED", "RESEARCHED", "RANKED", state.value) \
            if ranking is not None else ("DISCOVERED", "RESEARCHED", state.value)
        return AIResearchOpportunity(
            opportunity_id=str(uuid4()), assessment=assessment, state=state,
            ranking=ranking, lifecycle=lifecycle, created_at=created_at,
        )

    def _persist(self, opportunity: AIResearchOpportunity) -> AIResearchOpportunity:
        self.repository.save_opportunity(opportunity)
        return opportunity

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _result_time_is_valid(created_at: datetime, assessed_at: datetime) -> bool:
        return (
            isinstance(created_at, datetime)
            and created_at.tzinfo is not None
            and created_at.utcoffset() is not None
            and created_at.astimezone(timezone.utc) <= assessed_at
        )

    @staticmethod
    def _parse_time(value: Any, name: str) -> datetime:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value)
            except ValueError as exc:
                raise CandidateAssessmentError(f"invalid {name}") from exc
        else:
            raise CandidateAssessmentError(f"invalid {name}")
        if result.tzinfo is None or result.utcoffset() is None:
            raise CandidateAssessmentError(f"{name} must be timezone-aware")
        return result.astimezone(timezone.utc)

    @staticmethod
    def _score(scores: Mapping[str, Any], key: str) -> float:
        value = scores.get(key)
        if isinstance(value, Mapping):
            value = value.get("score")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not isfinite(number):
            return 0.0
        return max(-1.0, min(1.0, number))

    @staticmethod
    def _directional_alignment(bias: DirectionalBias, *scores: float) -> float:
        if not scores or bias is DirectionalBias.INSUFFICIENT_EVIDENCE:
            return 0.0
        average = sum(scores) / len(scores)
        if bias is DirectionalBias.BULLISH:
            return (average + 1.0) / 2.0
        if bias is DirectionalBias.BEARISH:
            return (1.0 - average) / 2.0
        return 1.0 - abs(average)
