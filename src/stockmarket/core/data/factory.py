"""Construction of supported provider implementations for the platform."""

from __future__ import annotations

import os
from typing import Mapping

from ..markets import MarketRegistry
from ..models import Instrument
from ..security import get_secret
from .mock import MockProvider
from .provider import MarketDataProvider
from .twelvedata import TwelveDataProvider
from .yahoo import YahooProvider


def create_market_data_provider(
    name: str,
    instruments: Mapping[str, Instrument],
    *,
    markets: MarketRegistry,
    env: Mapping[str, str] | None = None,
) -> MarketDataProvider:
    """Create an explicitly supported provider; all current choices are research-only."""
    if name == "mock":
        return MockProvider(instruments, markets=markets)
    if name == "yahoo":
        return YahooProvider(instruments, markets=markets)
    if name == "twelvedata":
        env = os.environ if env is None else env
        key = get_secret("TWELVEDATA_API_KEY", env, required=False)
        if key is None:
            raise ValueError(
                "DATA_PROVIDER=twelvedata requires TWELVEDATA_API_KEY or TWELVEDATA_API_KEY_FILE")
        try:
            rpm = int(env.get("TWELVEDATA_REQUESTS_PER_MINUTE") or 8)
        except ValueError:
            raise ValueError("TWELVEDATA_REQUESTS_PER_MINUTE must be an integer") from None
        return TwelveDataProvider(
            instruments, key.reveal(), markets=markets, requests_per_minute=rpm)
    raise ValueError(
        f"unsupported DATA_PROVIDER {name!r}; choose 'mock', 'yahoo' or 'twelvedata'")
