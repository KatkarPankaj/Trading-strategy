"""Validated, instrument-scoped research evidence for deterministic aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
from typing import Literal, Protocol, Sequence

from .ai.analyst import AIAnalyst, news_score
from .ai.schemas import ResearchScoreSchema
from .models import Instrument
from ..news import NewsEvent, NewsProvider, NewsQuery

RESEARCH_COMPONENTS = frozenset({
    "volume",
    "momentum",
    "sector",
    "news",
    "fundamental",
    "history",
})


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    """A bounded research score with explicit instrument, source and observation time."""

    instrument_id: str
    component: str
    score: float
    observed_at: datetime
    source: str
    history_trades: int = 0
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        for name in ("instrument_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.component, str) or self.component not in RESEARCH_COMPONENTS:
            raise ValueError(
                f"component must be one of {sorted(RESEARCH_COMPONENTS)}")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) \
                or not isfinite(self.score) or not -1 <= self.score <= 1:
            raise ValueError("score must be finite and between -1 and 1")
        if not isinstance(self.observed_at, datetime) \
                or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime")
        if isinstance(self.history_trades, bool) or not isinstance(self.history_trades, int) \
                or self.history_trades < 0:
            raise ValueError("history_trades must be a non-negative integer")
        if self.max_age is not None and (
                not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0)):
            raise ValueError("max_age must be a positive timedelta or None")


class NewsResearchUnavailable(RuntimeError):
    """The configured news source failed or returned data outside its contract."""


@dataclass(frozen=True, slots=True)
class ResearchEvidenceCollection:
    evidence: tuple[ResearchEvidence, ...]
    event_count: int
    analyzed_count: int
    warnings: tuple[str, ...] = ()


NewsEvidenceCollection = ResearchEvidenceCollection


class ResearchEvidenceSource(Protocol):
    def collect(
        self,
        instrument: Instrument,
        *,
        as_of: datetime,
    ) -> ResearchEvidenceCollection:
        ...


class NewsEvidenceProducer:
    """Turn recent, instrument-specific news into one bounded AI research score."""

    def __init__(
        self,
        provider: NewsProvider,
        analyst: AIAnalyst,
        *,
        max_age: timedelta = timedelta(hours=24),
        limit: int = 20,
    ) -> None:
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        self.provider = provider
        self.analyst = analyst
        self.max_age = max_age
        self.limit = limit

    def collect(
        self,
        instrument: Instrument,
        *,
        as_of: datetime,
    ) -> ResearchEvidenceCollection:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        query = NewsQuery(
            symbols=(instrument.symbol,),
            affected_market=instrument.market,
            start_time=as_of - self.max_age,
            end_time=as_of,
            limit=self.limit,
        )
        try:
            events = self.provider.get_news(query)
        except Exception as exc:
            raise NewsResearchUnavailable(
                f"news provider failed: {type(exc).__name__}") from exc
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise NewsResearchUnavailable("news provider returned an invalid event collection")
        if len(events) > self.limit:
            raise NewsResearchUnavailable("news provider exceeded the requested result limit")

        seen_ids: set[object] = set()
        validated_events: list[NewsEvent] = []
        for event in events:
            if not isinstance(event, NewsEvent):
                raise NewsResearchUnavailable("news provider returned a non-NewsEvent value")
            if event.event_id in seen_ids:
                raise NewsResearchUnavailable("news provider returned duplicate event identifiers")
            seen_ids.add(event.event_id)
            if event.symbol != instrument.symbol:
                raise NewsResearchUnavailable("news provider returned an instrument-mismatched event")
            if event.affected_market is not None and event.affected_market != instrument.market:
                raise NewsResearchUnavailable("news provider returned a market-mismatched event")
            age = as_of - event.timestamp
            if age < timedelta(0) or age > self.max_age:
                raise NewsResearchUnavailable("news provider returned a future or stale event")
            validated_events.append(event)

        scores: list[float] = []
        analyzed_ids: list[str] = []
        warnings: list[str] = []
        for event in validated_events:
            result, _ = self.analyst.analyze_news_event(event)
            score = news_score(result)
            if score is None:
                warnings.append(
                    f"NEWS_ANALYSIS_UNAVAILABLE:{result.error or 'NO_DIRECTIONAL_SCORE'}")
                continue
            scores.append(score)
            analyzed_ids.append(str(event.event_id))

        evidence: tuple[ResearchEvidence, ...] = ()
        if scores:
            score = sum(scores) / len(scores)
            event_hash = sha256(",".join(analyzed_ids).encode("utf-8")).hexdigest()[:16]
            evidence = (
                ResearchEvidence(
                    instrument_id=instrument.instrument_id,
                    component="news",
                    score=score,
                    observed_at=max(
                        event.timestamp for event in validated_events
                        if str(event.event_id) in analyzed_ids
                    ),
                    source=f"ai_news:{event_hash}",
                    max_age=self.max_age,
                ),
            )
        elif not events:
            warnings.append("NO_MATCHING_NEWS_EVENTS")

        return ResearchEvidenceCollection(
            evidence=evidence,
            event_count=len(events),
            analyzed_count=len(scores),
            warnings=tuple(warnings),
        )


@dataclass(frozen=True, slots=True)
class ResearchObservation:
    """Source-supplied sector or fundamental facts for one instrument and observation time."""

    instrument_id: str
    market: str
    component: Literal["sector", "fundamental"]
    subject: str
    content: str
    observed_at: datetime
    source: str
    reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("instrument_id", "market", "subject", "content", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.component not in ("sector", "fundamental"):
            raise ValueError("component must be 'sector' or 'fundamental'")
        if len(self.content) > 6000:
            raise ValueError("content must not exceed 6000 characters")
        if not isinstance(self.observed_at, datetime) \
                or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime")
        if self.reference is not None and (
                not isinstance(self.reference, str) or not self.reference.strip()):
            raise ValueError("reference must be None or a non-empty string")


class ResearchObservationProvider(Protocol):
    """Returns one normalized, cited research snapshot; vendor adapters stay outside core."""

    name: str

    def get_observation(
        self,
        instrument: Instrument,
        *,
        component: Literal["sector", "fundamental"],
        as_of: datetime,
        max_age: timedelta,
    ) -> ResearchObservation | None:
        ...


class ResearchEvidenceUnavailable(RuntimeError):
    """A sector/fundamental provider failed or broke the evidence contract."""


class _ResearchEvidenceProducer:
    def __init__(
        self,
        provider: ResearchObservationProvider,
        analyst: AIAnalyst,
        *,
        component: Literal["sector", "fundamental"],
        max_age: timedelta,
    ) -> None:
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")
        self.provider = provider
        self.analyst = analyst
        self.component = component
        self.max_age = max_age

    def collect(
        self,
        instrument: Instrument,
        *,
        as_of: datetime,
    ) -> ResearchEvidenceCollection:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        try:
            observation = self.provider.get_observation(
                instrument,
                component=self.component,
                as_of=as_of,
                max_age=self.max_age,
            )
        except Exception as exc:
            raise ResearchEvidenceUnavailable(
                f"{self.component} provider failed: {type(exc).__name__}") from exc
        if observation is None:
            return ResearchEvidenceCollection(
                evidence=(), event_count=0, analyzed_count=0,
                warnings=(f"NO_{self.component.upper()}_OBSERVATION",))
        if not isinstance(observation, ResearchObservation):
            raise ResearchEvidenceUnavailable(
                f"{self.component} provider returned an invalid observation")
        if observation.instrument_id != instrument.instrument_id \
                or observation.market != instrument.market:
            raise ResearchEvidenceUnavailable(
                f"{self.component} provider returned a mismatched instrument or market")
        if observation.component != self.component:
            raise ResearchEvidenceUnavailable(
                f"{self.component} provider returned a mismatched component")
        age = as_of - observation.observed_at
        if age < timedelta(0) or age > self.max_age:
            raise ResearchEvidenceUnavailable(
                f"{self.component} provider returned a future or stale observation")

        result = self.analyst.analyze(
            "research_scoring",
            observation.content,
            context={
                "instrument_id": instrument.instrument_id,
                "symbol": instrument.symbol,
                "market": instrument.market,
                "component": observation.component,
                "subject": observation.subject,
                "source": observation.source,
                "reference": observation.reference,
                "observed_at": observation.observed_at.isoformat(),
                "as_of": as_of.isoformat(),
            },
        )
        if not result.ok or not isinstance(result.output, ResearchScoreSchema):
            return ResearchEvidenceCollection(
                evidence=(), event_count=1, analyzed_count=0,
                warnings=(f"{self.component.upper()}_ANALYSIS_UNAVAILABLE:"
                          f"{result.error or 'INVALID_RESEARCH_SCORE'}",))
        output = result.output
        identity = {
            "instrument_id": observation.instrument_id,
            "component": observation.component,
            "subject": observation.subject,
            "content": observation.content,
            "observed_at": observation.observed_at.isoformat(),
            "source": observation.source,
            "reference": observation.reference,
            "prompt_hash": result.prompt_hash,
            "response_hash": result.response_hash,
        }
        provenance = sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        evidence = ResearchEvidence(
            instrument_id=instrument.instrument_id,
            component=self.component,
            score=output.directional_score * output.confidence,
            observed_at=observation.observed_at,
            source=f"ai_{self.component}:{provenance}",
            max_age=self.max_age,
        )
        warnings = tuple(
            f"{self.component.upper()}_DATA_GAP:{gap}" for gap in output.data_gaps)
        return ResearchEvidenceCollection(
            evidence=(evidence,), event_count=1, analyzed_count=1, warnings=warnings)


class SectorEvidenceProducer(_ResearchEvidenceProducer):
    """AI-assess a source-provided sector observation for one instrument."""

    def __init__(
        self,
        provider: ResearchObservationProvider,
        analyst: AIAnalyst,
        *,
        max_age: timedelta = timedelta(days=1),
    ) -> None:
        super().__init__(provider, analyst, component="sector", max_age=max_age)


class FundamentalEvidenceProducer(_ResearchEvidenceProducer):
    """AI-assess source-provided company fundamentals for one instrument."""

    def __init__(
        self,
        provider: ResearchObservationProvider,
        analyst: AIAnalyst,
        *,
        max_age: timedelta = timedelta(days=90),
    ) -> None:
        super().__init__(provider, analyst, component="fundamental", max_age=max_age)
