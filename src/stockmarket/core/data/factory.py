"""Construction of supported provider implementations for the platform."""

from __future__ import annotations

from typing import Mapping

from ..markets import MarketRegistry
from ..models import Instrument
from .mock import MockProvider
from .provider import MarketDataProvider
from .yahoo import YahooProvider


def create_market_data_provider(
    name: str,
    instruments: Mapping[str, Instrument],
    *,
    markets: MarketRegistry,
) -> MarketDataProvider:
    """Create an explicitly supported provider; all current choices are research-only."""
    if name == "mock":
        return MockProvider(instruments, markets=markets)
    if name == "yahoo":
        return YahooProvider(instruments, markets=markets)
    raise ValueError(f"unsupported DATA_PROVIDER {name!r}; choose 'mock' or 'yahoo'")
