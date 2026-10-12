"""Declared capabilities of each supported market-data provider, and pre-call request validation.

The registry describes what an adapter in this repository can technically request. It is not a
statement about any account's subscription: entitlements are only known when configured explicitly
(for example ``TWELVEDATA_ENTITLED_EXCHANGES``) or reported by the provider at call time.

Live eligibility is an allowlist. A provider that is unknown, research-only or not explicitly marked
live-eligible can never feed live trading; currently no provider is live-eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from ..models import Instrument
from .provider import (
    INTERVALS,
    EntitlementMissing,
    HistoryDepthUnavailable,
    UnsupportedInterval,
    UnsupportedMarket,
)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    name: str
    research_only: bool
    live_eligible: bool
    markets: frozenset[str] | None  # None: any market defined in the registry
    exchanges: frozenset[str] | None  # None: any exchange (MIC)
    intervals: frozenset[str]
    streaming: bool
    credentials: tuple[str, ...]
    # Oldest bar the adapter can request, per interval, measured back from "now". Absent: unknown.
    max_history: Mapping[str, timedelta]
    max_bars_per_request: int | None
    entitlement_note: str

    def __post_init__(self) -> None:
        if self.live_eligible and self.research_only:
            raise ValueError("a research-only provider cannot be live-eligible")
        unknown = self.intervals - set(INTERVALS)
        if unknown:
            raise ValueError(f"unknown intervals {sorted(unknown)}")


PROVIDER_CAPABILITIES: Mapping[str, ProviderCapabilities] = {
    "mock": ProviderCapabilities(
        name="mock", research_only=True, live_eligible=False, markets=None, exchanges=None,
        intervals=frozenset(INTERVALS), streaming=False, credentials=(), max_history={},
        max_bars_per_request=None,
        entitlement_note="deterministic in-memory fixtures; not a market feed"),
    "yahoo": ProviderCapabilities(
        name="yahoo", research_only=True, live_eligible=False,
        markets=frozenset({"US", "IN", "DE"}),
        exchanges=frozenset({"XNYS", "XNAS", "ARCX", "XNSE", "XBOM", "XETR"}),
        intervals=frozenset(INTERVALS), streaming=False, credentials=(),
        # Yahoo's public chart endpoint only serves recent intraday history.
        max_history={"1m": timedelta(days=30), "5m": timedelta(days=60),
                     "15m": timedelta(days=60), "30m": timedelta(days=60),
                     "1h": timedelta(days=730)},
        max_bars_per_request=None,
        entitlement_note="unofficial public endpoint; no SLA, aggressive rate limiting"),
    "twelvedata": ProviderCapabilities(
        name="twelvedata", research_only=True, live_eligible=False,
        markets=frozenset({"US", "IN", "DE"}),
        exchanges=frozenset({"XNYS", "XNAS", "ARCX", "XNSE", "XBOM", "XETR"}),
        intervals=frozenset(INTERVALS), streaming=False, credentials=("TWELVEDATA_API_KEY",),
        max_history={}, max_bars_per_request=5000,
        entitlement_note="exchange coverage and real-time access depend on the account plan; "
                         "set TWELVEDATA_ENTITLED_EXCHANGES to the MICs the plan includes"),
}

ENTITLEMENT_ENV = {"twelvedata": "TWELVEDATA_ENTITLED_EXCHANGES"}


def capabilities_for(name: str) -> ProviderCapabilities | None:
    return PROVIDER_CAPABILITIES.get((name or "").strip().lower())


def is_known_provider(name: str) -> bool:
    return capabilities_for(name) is not None


def is_live_eligible(name: str) -> bool:
    """Fail closed: only an explicitly registered, non-research, live-eligible provider qualifies."""
    caps = capabilities_for(name)
    return caps is not None and caps.live_eligible and not caps.research_only


def configured_entitlements(name: str, env: Mapping[str, str]) -> frozenset[str] | None:
    """Exchanges the operator declared the account is entitled to; None when not declared."""
    variable = ENTITLEMENT_ENV.get(name)
    raw = (env.get(variable) or "").strip() if variable else ""
    if not raw:
        return None
    values = frozenset(v.strip().upper() for v in raw.split(",") if v.strip())
    if not values:
        raise ValueError(f"{variable} must list at least one exchange MIC")
    return values


def validate_request(
    caps: ProviderCapabilities,
    instrument: Instrument,
    *,
    interval: str | None = None,
    start: datetime | None = None,
    now: datetime | None = None,
    entitled_exchanges: frozenset[str] | None = None,
) -> None:
    """Raise a coded DataUnavailable before any network call the provider cannot satisfy."""
    if caps.markets is not None and instrument.market not in caps.markets:
        raise UnsupportedMarket(f"{caps.name} does not serve market {instrument.market}")
    if caps.exchanges is not None and instrument.exchange not in caps.exchanges:
        raise UnsupportedMarket(f"{caps.name} does not serve exchange {instrument.exchange}")
    if entitled_exchanges is not None and instrument.exchange not in entitled_exchanges:
        raise EntitlementMissing(
            f"{caps.name} account is not configured as entitled to {instrument.exchange}")
    if interval is None:
        return
    if interval not in caps.intervals:
        raise UnsupportedInterval(f"{caps.name} does not supply interval {interval!r}")
    depth = caps.max_history.get(interval)
    if depth is not None and start is not None and now is not None and start < now - depth:
        raise HistoryDepthUnavailable(
            f"{caps.name} serves at most {depth.days} days of {interval} history")
