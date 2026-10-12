"""Construction of supported provider implementations for the platform."""

from __future__ import annotations

import os
from typing import Mapping

from ..markets import MarketRegistry
from ..models import Instrument
from ..security import get_secret
from .capabilities import configured_entitlements
from .mock import MockProvider
from .provider import MarketDataProvider
from .routing import RoutedMarketDataProvider
from .twelvedata import TwelveDataProvider
from .yahoo import YahooProvider


def create_market_data_provider(
    name: str,
    instruments: Mapping[str, Instrument],
    *,
    markets: MarketRegistry,
    env: Mapping[str, str] | None = None,
    routes: Mapping[str, str] | None = None,
) -> MarketDataProvider:
    """Create the configured provider; all current choices are research-only.

    With no per-market ``routes`` and no declared entitlements the plain provider is returned, so the
    single-provider configuration behaves exactly as before. Otherwise a router validates market,
    exchange, interval, history depth and declared entitlements before each call.
    """
    env = os.environ if env is None else env
    routes = {market.upper(): provider.lower() for market, provider in (routes or {}).items()}
    cache: dict[str, MarketDataProvider] = {}

    def build(provider_name: str) -> MarketDataProvider:
        if provider_name not in cache:
            cache[provider_name] = _create_single(provider_name, instruments, markets=markets, env=env)
        return cache[provider_name]

    default = build(name)
    entitlements = {
        n: configured_entitlements(n, env) for n in {name, *routes.values()}}
    if not routes and all(value is None for value in entitlements.values()):
        return default
    return RoutedMarketDataProvider(
        default, {market: build(provider) for market, provider in routes.items()},
        entitlements=entitlements)


def _create_single(
    name: str,
    instruments: Mapping[str, Instrument],
    *,
    markets: MarketRegistry,
    env: Mapping[str, str],
) -> MarketDataProvider:
    if name == "mock":
        return MockProvider(instruments, markets=markets)
    if name == "yahoo":
        return YahooProvider(instruments, markets=markets)
    if name == "twelvedata":
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
