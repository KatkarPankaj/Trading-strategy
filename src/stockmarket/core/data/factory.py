"""Construction of supported provider implementations for the platform."""

from __future__ import annotations

import os
import json
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


def market_provider_env(env: Mapping[str, str], market: str) -> dict[str, str]:
    """Resolve vendor credentials and quota configuration without changing canonical metadata."""
    scoped = dict(env)
    for key in ("TWELVEDATA_API_KEY", "TWELVEDATA_API_KEY_FILE",
                "TWELVEDATA_REQUESTS_PER_MINUTE"):
        if env.get(f"{key}_{market}"):
            scoped[key] = env[f"{key}_{market}"]
    # Never accidentally combine a market-specific file with a global inline secret.
    if env.get(f"TWELVEDATA_API_KEY_FILE_{market}"):
        scoped.pop("TWELVEDATA_API_KEY", None)
    if env.get(f"TWELVEDATA_API_KEY_{market}"):
        scoped.pop("TWELVEDATA_API_KEY_FILE", None)
    return scoped


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
    scoped_routes: dict[str, MarketDataProvider] = {}
    for market in markets.codes():
        provider_name = routes.get(market, name)
        if any(env.get(f"{key}_{market}") for key in (
                "TWELVEDATA_API_KEY", "TWELVEDATA_API_KEY_FILE",
                "TWELVEDATA_REQUESTS_PER_MINUTE")) and provider_name == "twelvedata":
            scoped_routes[market] = _create_single(
                provider_name, instruments, markets=markets, env=market_provider_env(env, market))
    entitlements = {
        n: configured_entitlements(n, env) for n in {name, *routes.values()}}
    if not routes and not scoped_routes and all(value is None for value in entitlements.values()):
        return default
    return RoutedMarketDataProvider(
        default, {**{market: build(provider) for market, provider in routes.items()}, **scoped_routes},
        entitlements=entitlements)


def _create_single(
    name: str,
    instruments: Mapping[str, Instrument],
    *,
    markets: MarketRegistry,
    env: Mapping[str, str],
) -> MarketDataProvider:
    mapping = json.loads(env.get(f"{name.upper()}_SYMBOL_MAP") or "{}")
    if not isinstance(mapping, dict) or any(
            key not in instruments or not isinstance(value, str) or not value.strip()
            or len(value) > 128 for key, value in mapping.items()):
        raise ValueError(f"{name.upper()}_SYMBOL_MAP must map canonical instrument IDs to nonempty vendor symbols")
    if name == "mock":
        return MockProvider(instruments, markets=markets)
    if name == "yahoo":
        return YahooProvider(instruments, markets=markets, symbol_map=mapping)
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
            instruments, key.reveal(), markets=markets, requests_per_minute=rpm,
            symbol_map=mapping)
    raise ValueError(
        f"unsupported DATA_PROVIDER {name!r}; choose 'mock', 'yahoo' or 'twelvedata'")
