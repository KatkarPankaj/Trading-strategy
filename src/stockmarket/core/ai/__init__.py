"""Optional AI research layer. Imports nothing from risk, orders, portfolio, brokers or execution."""

from .analyst import AIAnalyst, AIProvider, AIResult, AIUnavailable, NullProvider, news_score, sanitize
from .schemas import (
    TASKS,
    EarningsSchema,
    EventClassificationSchema,
    MarketSummarySchema,
    NewsAnalysisSchema,
    ResearchNoteSchema,
    SignalExplanationSchema,
)

__all__ = [
    "AIAnalyst",
    "AIProvider",
    "AIResult",
    "AIUnavailable",
    "EarningsSchema",
    "EventClassificationSchema",
    "MarketSummarySchema",
    "NewsAnalysisSchema",
    "NullProvider",
    "ResearchNoteSchema",
    "SignalExplanationSchema",
    "TASKS",
    "news_score",
    "sanitize",
]
