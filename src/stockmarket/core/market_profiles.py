"""Backend-only market selection over the existing registry and calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .market_session import MarketSession
from .markets import MarketDefinition, MarketRegistry, SessionPhase, UnknownMarket
from .scanner import MarketScanner
from .strategies.base import Strategy, StrategyMetadata

_EXCHANGE_NAMES = {
    "XNSE": "NSE", "XBOM": "BSE", "XNYS": "NYSE", "XNAS": "NASDAQ",
    "ARCX": "NYSE Arca", "XETR": "Xetra",
}
_MARKET_NAMES = {"IN": "India", "US": "US", "DE": "Germany"}


@dataclass(frozen=True, slots=True)
class MarketProfile:
    definition: MarketDefinition
    research_session: MarketSession | None

    @property
    def exchange(self) -> str:
        return "/".join(_EXCHANGE_NAMES.get(mic, mic) for mic in self.definition.mics)

    @property
    def label(self) -> str:
        return f"{_MARKET_NAMES.get(self.definition.code, self.definition.name)} - {self.exchange}"


def default_research_sessions(registry: MarketRegistry) -> dict[str, MarketSession]:
    """Research policy defaults, not new exchange hours or holiday coverage."""
    sessions = {}
    for code in registry.codes():
        definition = registry.get(code)
        opened = datetime.combine(date.min, definition.calendar.open_time)
        closed = datetime.combine(date.min, definition.calendar.close_time)
        sessions[code] = MarketSession(
            timezone=definition.timezone,
            market_open=opened.time(),
            market_close=closed.time(),
            opening_range_end=(opened + timedelta(minutes=15)).time(),
            entry_start=(opened + timedelta(minutes=30 if code == "IN" else 15)).time(),
            entry_cutoff=(closed - timedelta(minutes=30 if code == "IN" else 60)).time(),
            square_off=(closed - timedelta(minutes=10 if code == "IN" else 5)).time(),
            late_entry_start=min(time(12), (closed - timedelta(minutes=60)).time()),
        )
    return sessions


class MarketProfileService:
    """Resolve Auto/manual selection without changing execution-account settings."""

    def __init__(
        self, registry: MarketRegistry, scanner: MarketScanner,
        strategies: Mapping[str, Strategy], sessions: Mapping[str, MarketSession],
        *, data_provider: str, default_market: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        registry.get(default_market)
        self.registry, self.scanner = registry, scanner
        self.strategies, self.sessions = dict(strategies), dict(sessions)
        self.data_provider, self.default_market, self.clock = data_provider, default_market, clock

    def status(self, selected_market: str = "AUTO", *, as_of: datetime | None = None) -> dict[str, Any]:
        if not isinstance(selected_market, str):
            raise ValueError("selected_market must be a market code or AUTO")
        selected = selected_market.upper()
        timestamp = self.clock() if as_of is None else as_of
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("market status timestamp must be timezone-aware")
        profiles = [self._context(code, timestamp) for code in self.registry.codes()]
        by_code = {profile["market"]: profile for profile in profiles}
        if selected != "AUTO":
            if selected not in by_code:
                raise UnknownMarket(selected)
            resolved, reason = selected, "EXPLICIT_SELECTION"
        else:
            priority = {"OPEN": 0, "PRE_MARKET": 1, "POST_MARKET": 2}
            active = [profile for profile in profiles if profile["status"] in priority]
            if active:
                active.sort(key=lambda profile: (
                    priority[profile["status"]], profile["market"] != self.default_market,
                    profile["market"]))
                resolved, reason = active[0]["market"], "ACTIVE_SESSION_PRIORITY"
            else:
                resolved, reason = self.default_market, "NO_ACTIVE_SESSION_DEFAULT"
        return {
            **by_code[resolved], "selected_market": selected,
            "resolved_market": resolved, "resolution_reason": reason,
            "default_market": self.default_market, "profiles": profiles,
        }

    def _context(self, code: str, timestamp: datetime) -> dict[str, Any]:
        definition = self.registry.get(code)
        profile = MarketProfile(definition, self.sessions.get(code))
        local = timestamp.astimezone(ZoneInfo(definition.timezone))
        day = local.date()
        covered = definition.is_covered(day)
        if not covered:
            status, session = "UNSUPPORTED_CALENDAR", "UNSUPPORTED_CALENDAR"
        elif day in definition.calendar.holidays:
            status, session = "HOLIDAY", "CLOSED"
        else:
            phase = definition.phase(timestamp)
            session = phase.value
            status = "OPEN" if phase is SessionPhase.REGULAR else session
        universes = []
        for universe in self.scanner.list_universes():
            # A selected country must never expand to a cross-country universe.
            if not universe.active or tuple(market.upper() for market in universe.markets) != (code,):
                continue
            _, instruments = self.scanner.get_universe(universe.universe_id)
            universes.append({
                "universe_id": universe.universe_id, "name": universe.name,
                "instrument_count": len(instruments),
                "eligible_metadata_count": sum(
                    instrument.active and instrument.tradable for instrument in instruments),
            })
        preferred = next(
            (item["universe_id"] for item in universes
             if item["universe_id"] == f"{code}_LIQUID_DEVELOPMENT"),
            universes[0]["universe_id"] if universes else None)
        supported = []
        for name, strategy in self.strategies.items():
            metadata = getattr(strategy, "metadata", None)
            if isinstance(metadata, StrategyMetadata) and (
                "*" in metadata.supported_markets or code in metadata.supported_markets
            ):
                supported.append(name)
        session_config = profile.research_session
        return {
            "market": code, "label": profile.label, "name": definition.name,
            "exchange": profile.exchange, "mics": definition.mics,
            "currency": definition.currency, "timezone": definition.timezone,
            "as_of": timestamp.astimezone(timezone.utc).isoformat(),
            "local_timestamp": local.isoformat(),
            "status": status, "session": session, "calendar_covered": covered,
            "calendar_years": sorted(definition.calendar.covered_years)
            if definition.calendar.covered_years is not None else None,
            "regular_open": definition.calendar.open_time.isoformat(),
            "regular_close": definition.calendar.close_time_on(day).isoformat(),
            "research_session": {
                "opening_range_end": session_config.opening_range_end.isoformat(),
                "entry_start": session_config.effective_entry_start.isoformat(),
                "entry_cutoff": session_config.entry_cutoff.isoformat(),
                "square_off": session_config.square_off.isoformat(),
            } if session_config else None,
            "supported_strategies": sorted(supported), "universes": universes,
            "default_universe_id": preferred,
            "data_provider": self.data_provider, "research_only": True,
            "limitations": [
                message for condition, message in (
                    (not covered, "UNSUPPORTED_CALENDAR: no calendar coverage for the exchange-local year"),
                    (not any(item["instrument_count"] for item in universes),
                     "EMPTY_MARKET_UNIVERSE: import verified instrument metadata for this market"),
                    (session_config is None, "RESEARCH_SESSION_NOT_CONFIGURED"),
                ) if condition
            ],
        }
