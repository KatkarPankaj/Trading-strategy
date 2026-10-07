"""Optional AI research layer. Imports nothing from risk, orders, portfolio, brokers or execution."""

from .analyst import (
    AIAnalyst,
    AIProvider,
    AIResult,
    AIUnavailable,
    NullProvider,
    StrategyRank,
    StrategySelection,
    build_strategy_selection,
    news_score,
    sanitize,
)
from .openai_compatible import OpenAICompatibleProvider
from .schemas import (
    TASKS,
    CandidateAssessmentSchema,
    EarningsSchema,
    EventClassificationSchema,
    MarketSummarySchema,
    NewsAnalysisSchema,
    ResearchNoteSchema,
    ResearchScoreSchema,
    SignalExplanationSchema,
    StrategyRankSchema,
    StrategySelectionSchema,
)

__all__ = [
    "AIAnalyst",
    "AIProvider",
    "AIResult",
    "AIUnavailable",
    "CandidateAssessmentSchema",
    "EarningsSchema",
    "EventClassificationSchema",
    "MarketSummarySchema",
    "NewsAnalysisSchema",
    "NullProvider",
    "OpenAICompatibleProvider",
    "ResearchNoteSchema",
    "ResearchScoreSchema",
    "SignalExplanationSchema",
    "StrategyRank",
    "StrategyRankSchema",
    "StrategySelection",
    "StrategySelectionSchema",
    "TASKS",
    "build_strategy_selection",
    "news_score",
    "sanitize",
]
