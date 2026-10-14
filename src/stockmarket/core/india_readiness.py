"""Fail-closed India prerequisites. Observation is never execution approval."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .data.provider import expected_provider_name
from .markets import MarketRegistry
from .models import AssetClass, Instrument, TradingStatus
from .portfolio import PortfolioManager
from .recovery import RecoveryManager


class IndiaPaperReadiness:
    def __init__(self, *, execution_market: str, instruments: Mapping[str, Instrument],
                 markets: MarketRegistry, portfolio: PortfolioManager, provider: object, recovery: RecoveryManager,
                 starting_cash_explicit: bool, clock: Callable[[], datetime]) -> None:
        self.execution_market = execution_market
        self.instruments = instruments
        self.markets = markets
        self.portfolio = portfolio
        self.provider = provider
        self.recovery = recovery
        self.starting_cash_explicit = starting_cash_explicit
        self.clock = clock

    def instrument_status(self, identity: str) -> dict[str, Any]:
        instrument = self.instruments[identity]
        now = self.clock()
        day = now.astimezone(ZoneInfo(instrument.timezone)).date()
        definition = self.markets.get(instrument.market)
        calendar = definition.exchange_calendars.get(instrument.exchange)
        covered = calendar is not None and calendar.review_required \
            and calendar.is_covered(day) and calendar.review_expires is not None \
            and day <= calendar.review_expires
        metadata = instrument.market == "IN" and instrument.exchange in {"XNSE", "XBOM"} \
            and instrument.instrument_id == f"{instrument.exchange}:{instrument.symbol}" \
            and instrument.currency == "INR" and instrument.timezone == "Asia/Kolkata" \
            and instrument.asset_class is AssetClass.EQUITY \
            and instrument.active and instrument.tradable \
            and instrument.trading_status is TradingStatus.ACTIVE \
            and not (instrument.name or "").startswith("DEVELOPMENT")
        cash = self.portfolio.cash.get("INR", 0)
        funding = self.starting_cash_explicit and self.portfolio.base_currency == "INR" \
            and isinstance(cash, (int, float)) and isfinite(cash) and cash > 0
        recovery = self.recovery.execution_ready
        # No currently registered adapter supplies independently validated India execution evidence.
        checks = {
            "observation_enabled": True,
            "calendar_verified": bool(covered),
            "instrument_metadata_valid": bool(metadata),
            "instrument_mapping_verified": False,
            "provider_validated": False,
            "funding_currency_validated": funding,
            "paper_recovery_complete": recovery,
            "worker_market_matches": self.execution_market == "IN",
            "regular_session": definition.phase(now, instrument.exchange).value == "REGULAR",
        }
        reasons = []
        codes = {
            "calendar_verified": "INDIA_CALENDAR_MISSING_UNREVIEWED_OR_OUT_OF_COVERAGE",
            "instrument_metadata_valid": "INDIA_INSTRUMENT_METADATA_INVALID",
            "instrument_mapping_verified": "INDIA_MAPPING_NOT_INDEPENDENTLY_VERIFIED",
            "provider_validated": "INDIA_EXECUTION_PROVIDER_NOT_VALIDATED",
            "funding_currency_validated": "EXPLICIT_INR_BASE_CASH_REQUIRED_NO_IMPLICIT_FX",
            "paper_recovery_complete": "PAPER_RECOVERY_INCOMPLETE",
            "worker_market_matches": "EXECUTION_MARKET_MISMATCH",
            "regular_session": "OUTSIDE_VERIFIED_EXCHANGE_SESSION",
        }
        for key, code in codes.items():
            if not checks[key]:
                reasons.append(code)
        return {
            "instrument_id": identity, "execution_market": self.execution_market,
            "checks": checks, "execution_eligible": not reasons,
            "status": "BLOCKED" if reasons else "PER_ORDER_VALIDATION_REQUIRED",
            "blockers": reasons, "provider": expected_provider_name(self.provider, instrument),
            "quote_status": "PER_ORDER_VALIDATION_REQUIRED", "risk_approval": False,
            "cross_currency_funding": "UNSUPPORTED", "execution": "NOT_SUBMITTED",
        }

    def blocker(self, identity: str) -> str | None:
        instrument = self.instruments.get(identity)
        if instrument is None:
            return "INSTRUMENT_NOT_REGISTERED"
        if instrument.market != self.execution_market:
            return "EXECUTION_MARKET_MISMATCH"
        if instrument.market == "IN":
            return "; ".join(self.instrument_status(identity)["blockers"]) or None
        return None

    def status(self) -> dict[str, Any]:
        india = [self.instrument_status(i.instrument_id) for i in self.instruments.values()
                 if i.market == "IN"]
        return {"execution_market": self.execution_market, "scheduling_scope": "SINGLE_MARKET",
                "india": india, "india_status": "BLOCKED" if not india or any(
                    not row["execution_eligible"] for row in india) else "PER_ORDER_VALIDATION_REQUIRED",
                "simultaneous_market_workers": "NOT_CERTIFIED"}
