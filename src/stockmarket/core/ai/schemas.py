"""Strict output schemas for AI tasks. Unknown fields are rejected, so an output cannot smuggle in an action."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...news.models import MarketImpact, NewsEventType, NewsSentiment

_Text = Field(min_length=1, max_length=1000)
_Item = Field(min_length=1, max_length=300)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewsAnalysisSchema(_Strict):
    summary: str = _Text
    event_type: NewsEventType
    sentiment: NewsSentiment
    sentiment_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    market_impact: MarketImpact
    relevance: float = Field(ge=0, le=1, allow_inf_nan=False)
    key_points: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)


class EarningsMetric(_Strict):
    name: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)
    period: str = Field(default="", max_length=40)


class EarningsSchema(_Strict):
    summary: str = _Text
    result_vs_expectations: Literal["beat", "meet", "miss", "unknown"]
    guidance: Literal["raised", "maintained", "lowered", "none", "unknown"]
    key_metrics: list[EarningsMetric] = Field(
        default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=10)


class EventClassificationSchema(_Strict):
    event_type: NewsEventType
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    rationale: str = _Text


class MarketSummarySchema(_Strict):
    summary: str = _Text
    tone: Literal["positive", "negative", "mixed", "neutral", "unknown"]
    drivers: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)


class SignalExplanationSchema(_Strict):
    explanation: str = Field(min_length=1, max_length=2000)
    factors: list[str] = Field(default_factory=list, max_length=10)


class ResearchNoteSchema(_Strict):
    title: str = Field(min_length=1, max_length=200)
    thesis: str = Field(min_length=1, max_length=2000)
    supporting_points: list[str] = Field(default_factory=list, max_length=10)
    counterpoints: list[str] = Field(default_factory=list, max_length=10)
    data_gaps: list[str] = Field(default_factory=list, max_length=10)


class StrategyRankSchema(_Strict):
    strategy: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    rationale: str = Field(min_length=1, max_length=1000)


class StrategySelectionSchema(_Strict):
    summary: str = _Text
    ranked_strategies: list[StrategyRankSchema] = Field(
        min_length=1, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=10)
    data_gaps: list[str] = Field(default_factory=list, max_length=10)


TASKS: dict[str, tuple[type[_Strict], str]] = {
    "news_analysis": (NewsAnalysisSchema, "Analyse the news item and classify its likely market relevance."),
    "earnings": (EarningsSchema, "Extract the earnings result, guidance and key metrics from the text."),
    "event_classification": (EventClassificationSchema, "Classify the type of event described."),
    "market_summary": (MarketSummarySchema, "Summarise the market conditions described."),
    "signal_explanation": (SignalExplanationSchema,
                           "Explain in plain language why the supplied machine-generated signal data looks the way it does. "
                           "Do not change or second-guess the decision."),
    "research_note": (ResearchNoteSchema, "Write a short research note from the supplied material."),
    "strategy_selection": (
        StrategySelectionSchema,
        "Rank only the supplied deterministic strategy candidates for evaluation against the supplied market research. "
        "Do not invent strategies, produce trading signals, recommend a trade direction or size, or issue orders. "
        "Explain uncertainty and data gaps; the ranking is advisory and is not authorization to trade.",
    ),
}
