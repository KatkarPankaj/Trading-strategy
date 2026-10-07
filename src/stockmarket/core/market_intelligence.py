"""Proposal-only orchestration over the existing deterministic research pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Callable, Sequence
from uuid import UUID

from .aggregation import AggregatedAction
from .ai.analyst import StrategySelection
from .market_session import MarketSession
from .models import Instrument, SignalSide
from .regime import RegimeAssessment
from .research import ResearchEvidence
from .strategy_pipeline import (
    PipelineStatus,
    StrategyPipelineResult,
    StrategyResearchPipeline,
)

MAX_OPPORTUNITY_CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """Explainable advisory proposal; it is not a risk approval or an order."""

    proposal_id: str
    rank: int
    instrument_id: str
    symbol: str
    market: str
    as_of: datetime
    generated_at: datetime
    side: SignalSide
    strategy: str
    signal_id: UUID
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    opportunity_score: float
    aggregate_score: float
    aggregation_explanation: str
    regime: RegimeAssessment
    strategy_selection: StrategySelection
    research_evidence: tuple[ResearchEvidence, ...]
    research_warnings: tuple[str, ...]
    explanation: tuple[str, ...]
    risk_status: str = "NOT_EVALUATED"

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.instrument_id or not self.symbol \
                or not self.market or not self.strategy:
            raise ValueError("proposal identity fields must be non-empty")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("rank must be a positive integer")
        for name in ("as_of", "generated_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None \
                    or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.generated_at < self.as_of:
            raise ValueError("generated_at must not precede as_of")
        if self.side not in (SignalSide.BUY, SignalSide.SELL):
            raise ValueError("trade proposals must be directional")
        if isinstance(self.opportunity_score, bool) \
                or not isinstance(self.opportunity_score, (int, float)) \
                or not isfinite(self.opportunity_score) \
                or not 0 <= self.opportunity_score <= 100:
            raise ValueError("opportunity_score must be finite and between 0 and 100")
        if isinstance(self.aggregate_score, bool) \
                or not isinstance(self.aggregate_score, (int, float)) \
                or not isfinite(self.aggregate_score) \
                or not -1 <= self.aggregate_score <= 1:
            raise ValueError("aggregate_score must be finite and between -1 and 1")
        if not isinstance(self.aggregation_explanation, str) \
                or not self.aggregation_explanation.strip():
            raise ValueError("aggregation_explanation must be non-empty")
        if not isinstance(self.signal_id, UUID):
            raise ValueError("signal_id must be a UUID")
        if isinstance(self.entry_price, bool) \
                or not isinstance(self.entry_price, (int, float)) \
                or not isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("entry_price must be finite and positive")
        if self.strategy_selection.instrument_id != self.instrument_id \
                or self.strategy_selection.as_of != self.as_of:
            raise ValueError("strategy selection must match proposal identity and timestamp")
        if self.regime.instrument_id != self.instrument_id \
                or self.regime.assessed_at != self.as_of:
            raise ValueError("regime assessment must match proposal identity and timestamp")
        if any(item.instrument_id != self.instrument_id or item.observed_at > self.as_of
               for item in self.research_evidence):
            raise ValueError("proposal evidence must be instrument-matched and not future-dated")
        if self.risk_status != "NOT_EVALUATED":
            raise ValueError("research proposals cannot claim a risk decision")


@dataclass(frozen=True, slots=True)
class OpportunityAssessment:
    """Outcome for one candidate, including candidates that did not yield proposals."""

    instrument_id: str
    symbol: str
    status: str
    reason: str | None
    aggregate_action: str | None
    opportunity_score: float | None
    proposal_id: str | None


@dataclass(frozen=True, slots=True)
class MarketIntelligenceResult:
    as_of: datetime
    generated_at: datetime
    proposals: tuple[TradeProposal, ...]
    assessments: tuple[OpportunityAssessment, ...]
    submission_contexts: tuple["ProposalSubmissionContext", ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalSubmissionContext:
    proposal: TradeProposal
    pipeline_result: StrategyPipelineResult


class MarketIntelligenceOrchestrator:
    """Rank existing pipeline outcomes without owning signals, risk or execution."""

    def __init__(
        self,
        pipeline: StrategyResearchPipeline,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if pipeline.news_evidence_producer is None:
            raise ValueError(
                "market intelligence requires a configured news evidence producer")
        self._pipeline = pipeline
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        candidates: Sequence[tuple[Instrument, MarketSession]],
        *,
        as_of: datetime,
    ) -> MarketIntelligenceResult:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None \
                or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        started_at = self._clock()
        if not isinstance(started_at, datetime) or started_at.tzinfo is None \
                or started_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        if as_of > started_at:
            raise ValueError("as_of must not be in the future")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise TypeError("candidates must be a sequence of instrument/session pairs")
        if not 1 <= len(candidates) <= MAX_OPPORTUNITY_CANDIDATES:
            raise ValueError(
                f"candidates must contain between 1 and "
                f"{MAX_OPPORTUNITY_CANDIDATES} entries")

        instrument_ids: set[str] = set()
        outcomes: list[tuple[Instrument, StrategyPipelineResult]] = []
        for candidate in candidates:
            if not isinstance(candidate, tuple) or len(candidate) != 2:
                raise TypeError("each candidate must be an (Instrument, MarketSession) pair")
            instrument, session = candidate
            if not isinstance(instrument, Instrument) or not isinstance(session, MarketSession):
                raise TypeError("candidate pairs must contain an Instrument and MarketSession")
            if instrument.instrument_id in instrument_ids:
                raise ValueError(
                    f"duplicate candidate instrument {instrument.instrument_id!r}")
            instrument_ids.add(instrument.instrument_id)
            result = self._pipeline.run(instrument, session, as_of=as_of)
            outcomes.append((instrument, result))

        generated_at = self._clock()
        if not isinstance(generated_at, datetime) or generated_at.tzinfo is None \
                or generated_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        if generated_at < as_of:
            raise ValueError("generated_at must not precede as_of")

        ranked: list[tuple[float, float, str, Instrument, StrategyPipelineResult]] = []
        assessments: list[OpportunityAssessment] = []
        for instrument, result in outcomes:
            decision = result.decision
            signal = result.strategy_signal
            actionable = (
                result.status is PipelineStatus.COMPLETE
                and decision is not None
                and signal is not None
                and result.regime is not None
                and result.selection is not None
                and decision.action in (AggregatedAction.BUY, AggregatedAction.SELL)
                and signal.side is (
                    SignalSide.BUY if decision.action is AggregatedAction.BUY
                    else SignalSide.SELL)
                and signal.entry_price is not None
                and signal.timestamp <= as_of
            )
            if not actionable:
                reason = result.reason or (
                    ",".join(decision.reason_codes)
                    if decision is not None else "NON_ACTIONABLE_PIPELINE_RESULT")
                if not reason:
                    reason = "NON_ACTIONABLE_PIPELINE_RESULT"
                if result.research_warnings:
                    reason = ";".join((reason, *result.research_warnings))
                assessments.append(OpportunityAssessment(
                    instrument_id=instrument.instrument_id,
                    symbol=instrument.symbol,
                    status=(result.status.value if result.status is not PipelineStatus.COMPLETE
                            else "NO_PROPOSAL"),
                    reason=reason,
                    aggregate_action=decision.action.value if decision else None,
                    opportunity_score=decision.confidence if decision else None,
                    proposal_id=None,
                ))
                continue

            aggregate = decision.explanation.get("aggregate", {})
            score = aggregate.get("weighted_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) \
                    or not isfinite(score):
                assessments.append(OpportunityAssessment(
                    instrument_id=instrument.instrument_id,
                    symbol=instrument.symbol,
                    status="INVALID_AGGREGATION",
                    reason="AGGREGATE_SCORE_UNAVAILABLE",
                    aggregate_action=decision.action.value,
                    opportunity_score=None,
                    proposal_id=None,
                ))
                continue
            ranked.append((
                decision.confidence,
                abs(float(score)),
                instrument.instrument_id,
                instrument,
                result,
            ))

        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        proposals: list[TradeProposal] = []
        submission_contexts: list[ProposalSubmissionContext] = []
        for rank, (opportunity_score, aggregate_score, _, instrument, result) in enumerate(
                ranked, start=1):
            decision = result.decision
            signal = result.strategy_signal
            regime = result.regime
            selection = result.selection
            if decision is None or signal is None or regime is None or selection is None:
                raise RuntimeError("ranked opportunity lost its complete pipeline context")
            ranked_strategy = next(
                (item for item in selection.ranked_strategies
                 if item.strategy == signal.strategy),
                None,
            )
            explanations = [
                f"Deterministic aggregate action: {decision.action.value}.",
                f"Aggregate confidence: {decision.confidence:.2f}/100.",
                f"Weighted evidence score: {aggregate_score:.6f}.",
                f"Market regime: {regime.label.value} "
                f"(directional score {regime.directional_score:.6f}).",
                f"Selected deterministic strategy: {signal.strategy}.",
                f"AI strategy-selection summary: {selection.summary}",
            ]
            if ranked_strategy is not None:
                explanations.append(
                    f"Strategy rationale: {ranked_strategy.rationale}")
            explanations.extend(signal.reasons)
            explanations.extend(decision.reason_codes)
            explanations.extend(
                f"{item.component} evidence from {item.source} observed at "
                f"{item.observed_at.isoformat()} (score {item.score:.6f})."
                for item in result.research_evidence
            )
            proposal = TradeProposal(
                proposal_id=decision.input_hash,
                rank=rank,
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                market=instrument.market,
                as_of=as_of,
                generated_at=generated_at,
                side=signal.side,
                strategy=signal.strategy,
                signal_id=signal.signal_id,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                opportunity_score=opportunity_score,
                aggregate_score=aggregate_score,
                aggregation_explanation=decision.explanation_json(),
                regime=regime,
                strategy_selection=selection,
                research_evidence=result.research_evidence,
                research_warnings=result.research_warnings,
                explanation=tuple(
                    [*explanations,
                     *(f"Research warning: {warning}"
                       for warning in result.research_warnings)]),
            )
            proposals.append(proposal)
            submission_contexts.append(ProposalSubmissionContext(proposal, result))
            assessments.append(OpportunityAssessment(
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                status="PROPOSED",
                reason=None,
                aggregate_action=decision.action.value,
                opportunity_score=opportunity_score,
                proposal_id=proposal.proposal_id,
            ))

        return MarketIntelligenceResult(
            as_of=as_of,
            generated_at=generated_at,
            proposals=tuple(proposals),
            assessments=tuple(assessments),
            submission_contexts=tuple(submission_contexts),
        )
