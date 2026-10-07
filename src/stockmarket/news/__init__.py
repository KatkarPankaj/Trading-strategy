"""Provider-independent news events and market-intelligence contracts."""

from .models import (
    MarketImpact,
    NewsAnalysis,
    NewsEvent,
    NewsEventType,
    NewsQuery,
    NewsSentiment,
)
from .provider import NewsProvider
from .finnhub import FinnhubNewsProvider, FinnhubNewsUnavailable

__all__ = [
    "MarketImpact",
    "NewsAnalysis",
    "NewsEvent",
    "NewsEventType",
    "NewsProvider",
    "FinnhubNewsProvider",
    "FinnhubNewsUnavailable",
    "NewsQuery",
    "NewsSentiment",
]
