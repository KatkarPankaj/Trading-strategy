"""Typed news and structured research-analysis models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Any, Mapping
from uuid import UUID, uuid4


class NewsEventType(str, Enum):
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    ACQUISITION = "acquisition"
    MERGER = "merger"
    CONTRACT = "contract"
    PRODUCT_LAUNCH = "product_launch"
    REGULATORY = "regulatory"
    LAWSUIT = "lawsuit"
    MANAGEMENT_CHANGE = "management_change"
    ANALYST_ACTION = "analyst_action"
    MACROECONOMIC_EVENT = "macroeconomic_event"
    GEOPOLITICAL_EVENT = "geopolitical_event"
    SECTOR_EVENT = "sector_event"
    OTHER = "other"
    UNKNOWN = "unknown"


class NewsSentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class MarketImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NewsEvent:
    """Normalized news item, independent of any vendor or execution workflow."""

    timestamp: datetime
    source: str
    headline: str
    event_type: NewsEventType
    sentiment: NewsSentiment
    sentiment_confidence: float
    market_impact: MarketImpact
    relevance: float
    symbol: str | None = None
    content: str | None = None
    reference: str | None = None
    affected_sector: str | None = None
    affected_market: str | None = None
    provider_event_id: str | None = None
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_aware_datetime(self.timestamp, "timestamp")
        _require_non_empty(self.source, "source")
        _require_non_empty(self.headline, "headline")
        _require_enum(self.event_type, NewsEventType, "event_type")
        _require_enum(self.sentiment, NewsSentiment, "sentiment")
        _require_enum(self.market_impact, MarketImpact, "market_impact")
        _require_uuid(self.event_id, "event_id")
        _require_unit_interval(self.sentiment_confidence,
                               "sentiment_confidence")
        _require_unit_interval(self.relevance, "relevance")
        for name in ("symbol", "content", "reference", "affected_sector", "affected_market", "provider_event_id"):
            _require_optional_text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class NewsAnalysis:
    """Structured research output associated with a source event, never an order."""

    event_id: UUID
    analyzed_at: datetime
    summary: str
    event_type: NewsEventType
    sentiment: NewsSentiment
    sentiment_confidence: float
    market_impact: MarketImpact
    relevance: float
    key_points: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    analyzer: str | None = None
    analysis_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, "event_id")
        _require_uuid(self.analysis_id, "analysis_id")
        _require_aware_datetime(self.analyzed_at, "analyzed_at")
        _require_non_empty(self.summary, "summary")
        _require_enum(self.event_type, NewsEventType, "event_type")
        _require_enum(self.sentiment, NewsSentiment, "sentiment")
        _require_enum(self.market_impact, MarketImpact, "market_impact")
        _require_unit_interval(self.sentiment_confidence,
                               "sentiment_confidence")
        _require_unit_interval(self.relevance, "relevance")
        _require_text_tuple(self.key_points, "key_points")
        _require_text_tuple(self.risks, "risks")
        _require_optional_text(self.analyzer, "analyzer")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        event_id: UUID,
        analyzed_at: datetime,
    ) -> "NewsAnalysis":
        """Validate structured analysis fields and reject action/order payloads."""
        if not isinstance(payload, Mapping):
            raise TypeError("analysis payload must be a mapping")
        allowed = {
            "summary",
            "event_type",
            "sentiment",
            "sentiment_confidence",
            "market_impact",
            "relevance",
            "key_points",
            "risks",
            "analyzer",
        }
        unexpected = set(payload).difference(allowed)
        if unexpected:
            names = ", ".join(sorted(str(name) for name in unexpected))
            raise ValueError(
                f"unsupported analysis fields: {names}; analysis cannot contain execution commands"
            )
        required = {
            "summary",
            "event_type",
            "sentiment",
            "sentiment_confidence",
            "market_impact",
            "relevance",
        }
        missing = required.difference(payload)
        if missing:
            raise ValueError(
                f"analysis payload missing fields: {sorted(missing)}")
        try:
            event_type = NewsEventType(payload["event_type"])
            sentiment = NewsSentiment(payload["sentiment"])
            impact = MarketImpact(payload["market_impact"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"analysis payload contains an unsupported category: {exc}") from exc
        key_points = payload.get("key_points", ())
        risks = payload.get("risks", ())
        if isinstance(key_points, list):
            key_points = tuple(key_points)
        if isinstance(risks, list):
            risks = tuple(risks)
        return cls(
            event_id=event_id,
            analyzed_at=analyzed_at,
            summary=payload["summary"],
            event_type=event_type,
            sentiment=sentiment,
            sentiment_confidence=payload["sentiment_confidence"],
            market_impact=impact,
            relevance=payload["relevance"],
            key_points=key_points,
            risks=risks,
            analyzer=payload.get("analyzer"),
        )


@dataclass(frozen=True, slots=True)
class NewsQuery:
    """Provider-neutral filters for requesting a bounded collection of news."""

    symbols: tuple[str, ...] = ()
    affected_market: str | None = None
    affected_sector: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        _require_text_tuple(self.symbols, "symbols")
        _require_optional_text(self.affected_market, "affected_market")
        _require_optional_text(self.affected_sector, "affected_sector")
        if self.start_time is not None:
            _require_aware_datetime(self.start_time, "start_time")
        if self.end_time is not None:
            _require_aware_datetime(self.end_time, "end_time")
        if self.start_time is not None and self.end_time is not None:
            if self.end_time < self.start_time:
                raise ValueError("end_time must not precede start_time")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if self.limit <= 0:
            raise ValueError("limit must be positive")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_text(value: str | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{field_name} must be None or a non-empty string")


def _require_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_enum(value: Enum, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")


def _require_unit_interval(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be finite and between 0 and 1")


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field_name} must be a tuple of non-empty strings")
