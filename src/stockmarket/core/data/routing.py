"""Per-market provider routing with capability and entitlement checks before every call."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping

import pandas as pd

from ..brokers import MarketStatus
from ..models import Instrument
from .capabilities import ProviderCapabilities, capabilities_for, validate_request
from .provider import MarketDataProvider, Quote, UnsupportedSymbol


class RoutedMarketDataProvider(MarketDataProvider):
    """Dispatches each instrument to the provider configured for its market.

    Quotes keep the routed provider's name as provenance; use ``provider_name_for`` to know which
    name an instrument's data must carry.
    """

    def __init__(
        self,
        default: MarketDataProvider,
        routes: Mapping[str, MarketDataProvider] | None = None,
        *,
        entitlements: Mapping[str, frozenset[str] | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._default = default
        self._routes = {market.upper(): provider for market, provider in (routes or {}).items()}
        self._entitlements = dict(entitlements or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        providers = [default, *self._routes.values()]
        names = sorted({p.name for p in providers})
        self.name = names[0] if len(names) == 1 else "routed:" + ",".join(
            f"{market}={provider.name}" for market, provider in sorted(self._routes.items()))
        # Any research-only leg makes the whole router research-only.
        self.research_only = any(getattr(p, "research_only", True) for p in providers)

    @property
    def routes(self) -> dict[str, str]:
        return {market: provider.name for market, provider in self._routes.items()}

    def provider_for(self, market: str) -> MarketDataProvider:
        return self._routes.get(market.upper(), self._default)

    def provider_name_for(self, instrument: Instrument) -> str:
        return self.provider_for(instrument.market).name

    def capabilities_for(self, market: str) -> ProviderCapabilities | None:
        return capabilities_for(self.provider_for(market).name)

    def get_instrument(self, symbol: str, market: str | None = None) -> Instrument:
        if market is not None:
            return self.provider_for(market).get_instrument(symbol, market)
        for provider in (self._default, *self._routes.values()):
            try:
                found = provider.get_instrument(symbol, None)
            except UnsupportedSymbol:
                continue
            return self.provider_for(found.market).get_instrument(symbol, found.market)
        raise UnsupportedSymbol(f"unknown instrument {symbol!r}")

    def get_quote(self, instrument: Instrument) -> Quote:
        provider = self._checked(instrument)
        return provider.get_quote(instrument)

    def get_ohlcv(self, instrument: Instrument, interval: str, start: datetime,
                  end: datetime) -> pd.DataFrame:
        provider = self._checked(instrument, interval=interval, start=start)
        return provider.get_ohlcv(instrument, interval, start, end)

    def get_market_status(self, market: str) -> MarketStatus:
        return self.provider_for(market).get_market_status(market)

    def _checked(self, instrument: Instrument, *, interval: str | None = None,
                 start: datetime | None = None) -> MarketDataProvider:
        provider = self.provider_for(instrument.market)
        caps = capabilities_for(provider.name)
        if caps is None:
            # Injected custom providers have no declared capabilities; only their own checks apply.
            return provider
        validate_request(caps, instrument, interval=interval, start=start, now=self._clock(),
                         entitled_exchanges=self._entitlements.get(provider.name))
        return provider
