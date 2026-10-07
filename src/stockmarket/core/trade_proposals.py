"""Persisted position sizing and risk evaluation for generated strategy signals."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from math import isfinite
from time import perf_counter
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from .markets import MarketRegistry, SessionPhase, UnknownMarket
from .models import (
    Instrument,
    OrderSide,
    OrderType,
    RiskDecision,
    RiskDecisionStatus,
    Signal,
    SignalSide,
)
from .portfolio import PortfolioError, PortfolioManager
from .risk import OrderIntent, RiskContext, RiskEngine
from .risk_portfolio import order_key_for
from .sizing import SizingLimits, size_position
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TradeProposalSettings:
    max_opportunity_age: timedelta = timedelta(minutes=5)
    max_market_data_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        for name in ("max_opportunity_age", "max_market_data_age"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
            if value > timedelta(days=7):
                raise ValueError(f"{name} must not exceed seven days")


class TradeProposalService:
    """Turns one persisted signal into a persisted risk decision and, if approved, proposal."""

    def __init__(
        self,
        *,
        autonomous_repository: Any,
        research_repository: Any,
        opportunity_repository: Any,
        signal_generation_repository: Any,
        signal_repository: Any,
        proposal_repository: Any,
        portfolio: PortfolioManager,
        risk_engine: RiskEngine,
        sizing_limits: SizingLimits,
        instruments: Mapping[str, Instrument],
        markets: MarketRegistry,
        order_manager: Any = None,
        market_stats: Callable[[str], Mapping[str, Any]] | None = None,
        sector_of: Callable[[str], str | None] | None = None,
        settings: TradeProposalSettings = TradeProposalSettings(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.autonomous_repository = autonomous_repository
        self.research_repository = research_repository
        self.opportunity_repository = opportunity_repository
        self.signal_generation_repository = signal_generation_repository
        self.signal_repository = signal_repository
        self.proposal_repository = proposal_repository
        self.portfolio = portfolio
        self.risk_engine = risk_engine
        self.sizing_limits = sizing_limits
        self.instruments = dict(instruments)
        self.markets = markets
        self.order_manager = order_manager
        self.market_stats = market_stats or (lambda _: {})
        self.sector_of = sector_of or (lambda instrument_id: (
            self.instruments[instrument_id].sector
            if instrument_id in self.instruments else None
        ))
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(
        self,
        run_id: str,
        candidate_id: str,
        *,
        evaluation_as_of: datetime | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        now = self._now()
        evaluated_at = evaluation_as_of or now
        self._require_aware(evaluated_at, "evaluation_as_of")
        evaluated_at = evaluated_at.astimezone(timezone.utc)
        if evaluated_at > now:
            raise ValueError("evaluation_as_of must not be in the future")

        run = self.autonomous_repository.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown autonomous research run {run_id!r}")
        candidate = self.autonomous_repository.get_candidate(
            run_id, candidate_id)
        if candidate is None:
            raise KeyError(
                f"unknown candidate {candidate_id!r} in run {run_id!r}")
        instrument_id = candidate["instrument_id"]
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            raise ValueError("persisted signal instrument is not registered")

        generation = self.signal_generation_repository.latest_for_candidate(
            run_id, instrument_id)
        if generation is None or generation.get("status") != "SIGNAL_GENERATED":
            raise ValueError(
                "candidate has no persisted SIGNAL_GENERATED result")
        signal_id_value = generation.get("signal_id")
        if not signal_id_value:
            raise ValueError(
                "persisted signal generation has no signal identity")
        signal_row = self.signal_repository.get(signal_id_value)
        if signal_row is None:
            raise ValueError("persisted generated signal record is missing")
        if (signal_row.get("instrument_id") != instrument.instrument_id
                or signal_row.get("symbol") != instrument.symbol
                or signal_row.get("strategy") != generation.get("strategy_name")
                or signal_row.get("signal_id") != signal_id_value):
            raise ValueError(
                "persisted generated signal identity does not match its research candidate")
        signal = self._signal_from_row(signal_row)

        if signal.side is SignalSide.HOLD:
            return {
                "status": "NON_ACTIONABLE",
                "reason": "NO_SIGNAL: HOLD signals cannot create a TradeProposal.",
                "signal_id": str(signal.signal_id),
                "execution": "NOT_SUBMITTED",
                "trade_proposal": None,
                "duration_seconds": perf_counter() - started,
            }

        existing = self.proposal_repository.get_by_signal(
            str(signal.signal_id))
        if existing is not None:
            return self._stored_result(
                existing["evaluation"], existing, duplicate=True,
                started=started)

        context, rejection = self._load_context(
            run, candidate, generation, signal, instrument, evaluated_at)
        if rejection is not None:
            return self._reject(
                rejection,
                signal=signal,
                generation=generation,
                run_id=run_id,
                candidate_id=instrument_id,
                evaluated_at=evaluated_at,
                started=started,
                context=context,
            )

        assert context is not None
        account = self._account_context(instrument, evaluated_at)
        if isinstance(account, str):
            return self._reject(
                account, signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=context,
            )

        base_input = {
            **context,
            **account["provenance"],
            "signal_summary": {
                "instrument_id": signal.instrument_id,
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "side": signal.side.value,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_loss,
                "target_price": signal.take_profit,
            },
            "evaluation_as_of": evaluated_at,
            "risk_limits": self.risk_engine.limits,
            "portfolio_risk_disabled_controls": self.risk_engine.disabled_controls,
            "sizing_limits": self.sizing_limits,
        }
        fingerprint = self._fingerprint(base_input)
        key = self._fingerprint({
            "signal_id": signal.signal_id,
            "generation_id": generation["generation_id"],
            "input_fingerprint": fingerprint,
        })
        prior = self.proposal_repository.get_by_key(key)
        if prior is not None:
            return self._stored_result(prior, None, duplicate=True, started=started)

        if not instrument.active or not instrument.tradable:
            return self._reject(
                "INSTRUMENT_NOT_TRADABLE: instrument is not active and tradable.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if signal.side not in (SignalSide.BUY, SignalSide.SELL):
            return self._reject(
                "NON_ACTIONABLE_SIGNAL: only BUY and SELL signals are actionable.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if signal.entry_price is None or signal.stop_loss is None:
            return self._reject(
                "INVALID_STOP: a persisted entry and strategy stop are required.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if not instrument.is_valid_price(signal.entry_price) \
                or not instrument.is_valid_price(signal.stop_loss) \
                or (signal.take_profit is not None
                    and not instrument.is_valid_price(signal.take_profit)):
            return self._reject(
                "INVALID_SIGNAL_PRICE: entry, stop, and target must match instrument price constraints.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )

        position = account["positions"].get(instrument_id)
        if position is not None:
            return self._reject(
                "POSITION_ALREADY_OPEN: adding or reversing an existing position is not supported.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if account["open_orders_for_instrument"]:
            return self._reject(
                "OPEN_ORDER_EXISTS: an active order already targets this instrument.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if self.proposal_repository.has_approved_for_instrument(instrument_id):
            return self._reject(
                "UNSUBMITTED_PROPOSAL_EXISTS: a prior approved proposal for this instrument remains unsubmitted.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )

        entry = signal.entry_price
        stop = signal.stop_loss
        if signal.side is SignalSide.BUY and stop >= entry \
                or signal.side is SignalSide.SELL and stop <= entry:
            return self._reject(
                "INVALID_STOP: stop must be below entry for BUY and above entry for SELL.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )
        if signal.side is SignalSide.SELL and instrument.shortable is not True:
            return self._reject(
                "SHORTABILITY_UNCONFIRMED: a SELL entry requires explicit instrument shortability.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key,
            )

        fx_rate = account["fx_rate"]
        entry_base = entry * fx_rate
        stop_base = stop * fx_rate
        side = OrderSide.BUY if signal.side is SignalSide.BUY else OrderSide.SELL
        sector = self.sector_of(instrument_id)
        position_state = account["risk_state"]
        sizing = size_position(
            instrument,
            side,
            entry_price=entry_base,
            stop_price=stop_base,
            equity=self.portfolio.equity,
            available_cash=account["available_cash"],
            limits=self.sizing_limits,
            gross_exposure=position_state.gross_notional_exposure,
            sector_exposure=(
                position_state.sector_exposure.get(sector, 0.0)
                if sector is not None else 0.0
            ),
            current_position_notional=position_state.current_position_notional,
        )
        if not sizing.approved:
            return self._reject(
                f"POSITION_SIZING_REJECTED: {sizing.reason or 'quantity is zero'}.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key, sizing=sizing,
            )
        if not instrument.is_valid_order_quantity(sizing.quantity):
            return self._reject(
                "INVALID_QUANTITY: sized quantity violates instrument lot or minimum size.",
                signal=signal, generation=generation, run_id=run_id,
                candidate_id=instrument_id, evaluated_at=evaluated_at,
                started=started, context=base_input, fingerprint=fingerprint,
                idempotency_key=key, sizing=sizing,
            )

        base_signal = replace(
            signal,
            entry_price=entry_base,
            stop_loss=stop_base,
            take_profit=signal.take_profit * fx_rate
            if signal.take_profit is not None else None,
        )
        market_timezone = ZoneInfo(instrument.timezone)
        now_local_day = evaluated_at.astimezone(market_timezone).date()
        risk_context = RiskContext(
            assessed_at=evaluated_at,
            market_data_timestamp=context["data_timestamp"],
            market_data_valid=True,
            reference_price=entry_base,
            available_cash=account["available_cash"],
            estimated_fees=0.0,
            current_position_quantity=0,
            current_position_side=None,
            open_positions=account["open_positions"],
            trades_today=sum(
                1 for fill in self.portfolio.fills
                if fill.timestamp.astimezone(market_timezone).date()
                == now_local_day
            ),
            intent=OrderIntent.ENTRY,
            signal=base_signal,
            portfolio=position_state,
        )
        decision = self.risk_engine.evaluate_proposal(
            instrument,
            side=side,
            quantity=sizing.quantity,
            order_type=OrderType.MARKET,
            created_at=evaluated_at,
            context=risk_context,
            strategy=signal.strategy,
            signal_id=signal.signal_id,
        )
        decision = self._stable_decision(decision, key)
        if decision.status is RiskDecisionStatus.REJECTED:
            return self._persist_result(
                decision, signal, generation, run_id, instrument_id,
                evaluated_at, started, base_input, fingerprint, key, sizing,
            )

        notional = entry_base * sizing.quantity
        risk_amount = abs(entry_base - stop_base) * sizing.quantity
        proposal_id = uuid5(NAMESPACE_URL, f"trade-proposal:{key}")
        proposal = {
            "proposal_id": str(proposal_id),
            "signal_id": str(signal.signal_id),
            "generation_id": generation["generation_id"],
            "run_id": run_id,
            "candidate_id": instrument_id,
            "opportunity_id": context["opportunity_id"],
            "snapshot_id": context["snapshot_id"],
            "instrument_id": instrument.instrument_id,
            "symbol": instrument.symbol,
            "market": instrument.market,
            "asset_class": instrument.asset_class.value,
            "side": side.value,
            "quantity": sizing.quantity,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": signal.take_profit,
            "strategy": signal.strategy,
            "strategy_version": generation["strategy_version"],
            "account_currency": self.portfolio.base_currency,
            "instrument_currency": instrument.currency,
            "fx_rate_to_account": fx_rate,
            "fx_source": "PortfolioManager.rate_to_base (configured)",
            "fx_observed_at": None,
            "risk_amount": risk_amount,
            "risk_percentage": risk_amount / self.portfolio.equity,
            "position_exposure": notional,
            "required_cash": notional * (
                self.risk_engine.limits.cash_requirement_rate
                if self.risk_engine.limits is not None else 1.0),
            "portfolio_exposure": (
                position_state.gross_notional_exposure + notional),
            "sector": sector,
            "risk_decision_id": str(decision.decision_id),
            "risk_rule_results": [{
                "rule": "RiskEngine",
                "status": decision.status.value,
                "reason": decision.reason,
            }],
            "created_at": evaluated_at,
            "idempotency_key": key,
            "input_fingerprint": fingerprint,
            "provenance": base_input,
            "execution": "NOT_SUBMITTED",
        }
        return self._persist_result(
            decision, signal, generation, run_id, instrument_id,
            evaluated_at, started, base_input, fingerprint, key, sizing,
            proposal=proposal,
        )

    def get_latest(self, run_id: str, candidate_id: str) -> dict[str, Any] | None:
        candidate = self.autonomous_repository.get_candidate(
            run_id, candidate_id)
        if candidate is None:
            return None
        row = self.proposal_repository.latest_for_signal_candidate(
            run_id, candidate["instrument_id"])
        return row["payload"] if row is not None else None

    def _load_context(
        self,
        run: Mapping[str, Any],
        candidate: Mapping[str, Any],
        generation: Mapping[str, Any],
        signal: Signal,
        instrument: Instrument,
        evaluated_at: datetime,
    ) -> tuple[dict[str, Any] | None, str | None]:
        run_payload = run.get("payload")
        candidate_payload = candidate.get("payload")
        generation_payload = generation.get("payload")
        if not isinstance(run_payload, Mapping) \
                or not isinstance(candidate_payload, Mapping) \
                or not isinstance(generation_payload, Mapping):
            return None, "INVALID_PERSISTED_SIGNAL_CONTEXT: a persisted payload is malformed."
        request = run_payload.get("request")
        provenance = generation_payload.get("provenance")
        if not isinstance(request, Mapping) or not isinstance(provenance, Mapping) \
                or run.get("status") not in ("COMPLETE", "PARTIAL") \
                or run.get("stage") != "STRATEGY_SELECTED" \
                or request.get("mode") != "PAPER":
            return None, "INVALID_PERSISTED_SIGNAL_CONTEXT: run must be a completed PAPER research run."
        if candidate.get("stage") != "SIGNAL_GENERATED":
            return None, "INVALID_CANDIDATE_STATE: candidate is not SIGNAL_GENERATED."
        if (generation.get("run_id") != run.get("run_id")
                or generation.get("candidate_id") != instrument.instrument_id
                or generation.get("instrument_id") != instrument.instrument_id
                or generation.get("strategy_name") != signal.strategy
                or generation.get("signal_id") != str(signal.signal_id)
                or provenance.get("run_id") != run.get("run_id")
                or provenance.get("instrument_id") != instrument.instrument_id
                or provenance.get("strategy") != signal.strategy
                or candidate_payload.get("instrument_id") != instrument.instrument_id):
            return None, "PERSISTED_SIGNAL_IDENTITY_MISMATCH: signal, candidate, or run identities disagree."

        opportunity_id = candidate.get("opportunity_id")
        snapshot_id = candidate.get("snapshot_id")
        if not opportunity_id or not snapshot_id:
            return None, "PERSISTED_RESEARCH_LINK_MISSING: signal lacks opportunity or snapshot links."
        opportunity_row = self.opportunity_repository.get_opportunity(
            opportunity_id)
        snapshot_row = self.research_repository.get_snapshot(snapshot_id)
        if opportunity_row is None or snapshot_row is None:
            return None, "PERSISTED_RESEARCH_LINK_MISSING: opportunity or research snapshot is missing."
        opportunity = opportunity_row.get("payload")
        snapshot = snapshot_row.get("payload")
        if not isinstance(opportunity, Mapping) or not isinstance(snapshot, Mapping):
            return None, "INVALID_PERSISTED_RESEARCH_CONTEXT: opportunity or snapshot payload is malformed."
        assessment = opportunity.get("assessment")
        assessment_context = (
            assessment.get("input_context")
            if isinstance(assessment, Mapping) else None
        )
        if not isinstance(assessment, Mapping) \
                or not isinstance(assessment_context, Mapping) \
                or opportunity_row.get("state") != "STRATEGY_SELECTED" \
                or opportunity.get("state") != "STRATEGY_SELECTED" \
                or assessment.get("status") != "COMPLETE":
            return None, "INVALID_OPPORTUNITY_STATE: only a persisted STRATEGY_SELECTED opportunity is eligible."
        registered = assessment_context.get("registered_strategies")
        expected_version = next((
            item.get("version") for item in registered
            if isinstance(item, Mapping) and item.get("name") == signal.strategy
        ), None) if isinstance(registered, (tuple, list)) else None
        if (assessment.get("snapshot_id") != snapshot_id
                or assessment.get("instrument_id") != instrument.instrument_id
                or assessment.get("recommended_strategy") != signal.strategy
                or assessment_context.get("snapshot_id") != snapshot_id
                or assessment_context.get("snapshot_created_at")
                != snapshot.get("created_at")
                or snapshot_row.get("run_id") is None
                or snapshot_row.get("instrument_id") != instrument.instrument_id
                or snapshot.get("snapshot_id") != snapshot_id
                or snapshot.get("run_id") != snapshot_row.get("run_id")
                or snapshot.get("instrument_id") != instrument.instrument_id
                or snapshot.get("market") != instrument.market
                or opportunity_id != generation.get("opportunity_id")
                or snapshot_id != generation.get("snapshot_id")
                or not isinstance(candidate_payload.get("opportunity"), Mapping)
                or candidate_payload["opportunity"].get("opportunity_id")
                != opportunity_id
                or candidate_payload["opportunity"].get("state")
                != "STRATEGY_SELECTED"
                or expected_version != generation.get("strategy_version")):
            return None, "PERSISTED_RESEARCH_IDENTITY_MISMATCH: selected strategy/version or linked snapshot does not match the signal."
        if provenance.get("strategy_version") != generation.get("strategy_version"):
            return None, "STRATEGY_VERSION_MISMATCH: generated signal version differs from its persisted provenance."

        try:
            data_timestamp = self._parse_time(
                generation.get("data_timestamp"), "signal market-data timestamp")
            opportunity_as_of = self._parse_time(
                assessment_context.get("as_of"), "opportunity timestamp")
            run_as_of = self._parse_time(
                run.get("as_of"), "autonomous run timestamp")
            request_as_of = self._parse_time(
                request.get("as_of"), "autonomous request timestamp")
            snapshot_as_of = self._parse_time(
                snapshot.get("as_of"), "snapshot timestamp")
            signal_evaluation_as_of = self._parse_time(
                generation.get("evaluation_as_of"), "signal evaluation timestamp")
            signal_generated_at = self._parse_time(
                generation.get("generated_at"), "signal generation timestamp")
            opportunity_assessed_at = self._parse_time(
                assessment.get("assessed_at"), "opportunity assessment timestamp")
            opportunity_created_at = self._parse_time(
                opportunity.get("created_at"), "opportunity creation timestamp")
        except (TypeError, ValueError) as exc:
            return None, f"INVALID_PERSISTED_TIMESTAMP: {exc}."
        if signal.timestamp != data_timestamp:
            return None, "SIGNAL_DATA_TIMESTAMP_MISMATCH: signal is not tied to the persisted market-data timestamp."
        timestamps = (
            data_timestamp, signal.timestamp, opportunity_as_of, run_as_of,
            request_as_of, snapshot_as_of, signal_evaluation_as_of,
            signal_generated_at, opportunity_assessed_at,
            opportunity_created_at,
        )
        if any(value > evaluated_at for value in timestamps):
            return None, "FUTURE_DATED_SIGNAL_CONTEXT: persisted signal or research input is later than evaluation time."
        if (evaluated_at - data_timestamp > min(
                self.settings.max_market_data_age,
                self.risk_engine.limits.max_market_data_age
                if self.risk_engine.limits is not None
                else self.settings.max_market_data_age)
                or evaluated_at - opportunity_as_of > self.settings.max_opportunity_age):
            return None, "STALE_SIGNAL: signal market data or its opportunity snapshot is too old."
        if provenance.get("data_quality") != "VALID":
            return None, "INVALID_SIGNAL_DATA: signal generation did not record valid market data."
        try:
            market = self.markets.get(instrument.market)
        except UnknownMarket:
            return None, "UNKNOWN_MARKET: market definition is unavailable."
        local_day = evaluated_at.astimezone(ZoneInfo(market.timezone)).date()
        if not market.is_covered(local_day):
            return None, "MARKET_CALENDAR_UNKNOWN: calendar coverage is unavailable for evaluation date."
        if market.phase(evaluated_at) is not SessionPhase.REGULAR:
            return None, "MARKET_CLOSED: risk evaluation requires the configured regular market session."
        return {
            "opportunity_id": opportunity_id,
            "snapshot_id": snapshot_id,
            "data_timestamp": data_timestamp,
            "opportunity_as_of": opportunity_as_of,
            "strategy_version": generation["strategy_version"],
        }, None

    def _account_context(
        self, instrument: Instrument, evaluated_at: datetime,
    ) -> dict[str, Any] | str:
        try:
            fx_rate = self.portfolio.rate_to_base(instrument.currency)
        except PortfolioError as exc:
            return f"MISSING_FX: {exc}."
        if isinstance(fx_rate, bool) or not isinstance(fx_rate, (int, float)) \
                or not isfinite(fx_rate) or fx_rate <= 0:
            return "MISSING_FX: configured instrument-to-account FX rate is invalid."

        positions = self.portfolio.positions()
        risk_stats = dict(self.market_stats(instrument.instrument_id))
        order_history = (
            self.order_manager.orders() if self.order_manager is not None else ()
        )
        open_orders = (
            self.order_manager.open_orders()
            if self.order_manager is not None else ()
        )
        if self.order_manager is not None:
            risk_stats["orders_last_minute"] = sum(
                1 for order in order_history
                if timedelta(0) <= evaluated_at - order.timestamp <= timedelta(minutes=1)
            )
            risk_stats["recent_order_keys"] = frozenset(
                order_key_for(order.instrument_id, order.side.value,
                              order.signal_id, order.quantity)
                for order in order_history
                if timedelta(0) <= evaluated_at - order.timestamp <= timedelta(minutes=1)
            )

        reserved_exposure = 0.0
        reserved_cash = 0.0
        reserved_sectors: dict[str, float] = {}
        reserved_instruments: set[str] = set()
        for order in open_orders:
            remaining = order.remaining_quantity
            if remaining <= 0:
                continue
            reserved_instruments.add(order.instrument_id)
            order_instrument = self.instruments.get(order.instrument_id)
            if order_instrument is None:
                return "PENDING_ORDER_INSTRUMENT_UNKNOWN: cannot value an active order."
            reference_price = order.limit_price or order.stop_price
            if reference_price is None and order.signal_id is not None:
                linked_signal = self.signal_repository.get(
                    str(order.signal_id))
                if linked_signal is not None:
                    linked_payload = self._payload_mapping(
                        linked_signal.get("payload"))
                    reference_price = linked_payload.get("entry_price")
            if reference_price is None:
                return "PENDING_ORDER_PRICE_UNKNOWN: cannot reserve an active order without a persisted reference price."
            try:
                rate = self.portfolio.rate_to_base(order_instrument.currency)
            except PortfolioError as exc:
                return f"MISSING_FX_FOR_PENDING_ORDER: {exc}."
            notional = reference_price * remaining * rate
            reserved_exposure += notional
            sector_name = order_instrument.sector or "UNKNOWN"
            reserved_sectors[sector_name] = (
                reserved_sectors.get(sector_name, 0.0) + notional)
            reserved_cash += notional * (
                self.risk_engine.limits.cash_requirement_rate
                if self.risk_engine.limits is not None else 1.0
            )

        proposal_rows = self.proposal_repository.approved()
        reserved_proposal_exposure = 0.0
        reserved_proposal_cash = 0.0
        reserved_proposal_instruments: set[str] = set()
        for row in proposal_rows:
            proposal = row["payload"]
            if proposal.get("signal_id"):
                reserved_proposal_instruments.add(proposal["instrument_id"])
                reserved_proposal_exposure += float(
                    proposal.get("position_exposure", 0.0))
                reserved_proposal_cash += float(
                    proposal.get("required_cash", proposal.get(
                        "position_exposure", 0.0)))
                sector_name = proposal.get("sector") or "UNKNOWN"
                reserved_sectors[sector_name] = (
                    reserved_sectors.get(sector_name, 0.0)
                    + float(proposal.get("position_exposure", 0.0))
                )

        sector = self.sector_of(instrument.instrument_id)
        try:
            risk_state = self.portfolio.risk_state(
                instrument.instrument_id, sector=sector, **risk_stats)
        except PortfolioError as exc:
            return f"MISSING_FX: portfolio risk state cannot be valued: {exc}."
        risk_state = replace(
            risk_state,
            gross_notional_exposure=(
                risk_state.gross_notional_exposure
                + reserved_exposure + reserved_proposal_exposure),
            sector_exposure={
                **dict(risk_state.sector_exposure),
                **{
                    name: reserved_sectors.get(name, 0.0)
                    + risk_state.sector_exposure.get(name, 0.0)
                    for name in reserved_sectors
                },
            },
        )
        available_cash = max(
            0.0,
            self.portfolio.cash_base - reserved_cash - reserved_proposal_cash,
        )
        return {
            "fx_rate": fx_rate,
            "positions": positions,
            "open_positions": len(
                set(positions) | reserved_instruments
                | reserved_proposal_instruments),
            "open_orders_for_instrument": instrument.instrument_id in reserved_instruments,
            "available_cash": available_cash,
            "risk_state": risk_state,
            "provenance": {
                "equity": self.portfolio.equity,
                "cash": self.portfolio.cash_base,
                "available_cash": available_cash,
                "reserved_cash_from_open_orders": reserved_cash,
                "proposal_reserved_cash": reserved_proposal_cash,
                "instrument_currency": instrument.currency,
                "account_currency": self.portfolio.base_currency,
                "fx_rate_to_account": fx_rate,
                "fx_source": "PortfolioManager.rate_to_base (configured)",
                "fx_observed_at": None,
                "current_position": positions.get(instrument.instrument_id),
                "gross_exposure": risk_state.gross_notional_exposure,
                "sector_exposure": risk_state.sector_exposure,
                "risk_state": risk_state,
                "reserved_open_order_exposure": reserved_exposure,
                "unsubmitted_proposal_exposure": reserved_proposal_exposure,
            },
        }

    def _reject(
        self,
        reason: str,
        *,
        signal: Signal,
        generation: Mapping[str, Any],
        run_id: str,
        candidate_id: str,
        evaluated_at: datetime,
        started: float,
        context: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
        idempotency_key: str | None = None,
        sizing: Any = None,
    ) -> dict[str, Any]:
        decision_context = dict(context or {})
        decision_context.update({
            "signal_id": signal.signal_id,
            "generation_id": generation.get("generation_id"),
            "run_id": run_id,
            "candidate_id": candidate_id,
            "evaluation_as_of": evaluated_at,
            "sizing": sizing,
            "signal_summary": {
                "instrument_id": signal.instrument_id,
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "side": signal.side.value,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_loss,
                "target_price": signal.take_profit,
            },
        })
        fingerprint = fingerprint or self._fingerprint(decision_context)
        idempotency_key = idempotency_key or self._fingerprint({
            "signal_id": signal.signal_id,
            "generation_id": generation.get("generation_id"),
            "input_fingerprint": fingerprint,
        })
        decision = RiskDecision(
            RiskDecisionStatus.REJECTED,
            evaluated_at,
            reason,
            signal_id=signal.signal_id,
            decision_id=uuid5(
                NAMESPACE_URL, f"risk-decision:{idempotency_key}"),
        )
        return self._persist_result(
            decision, signal, generation, run_id, candidate_id, evaluated_at,
            started, decision_context, fingerprint, idempotency_key, sizing,
        )

    def _persist_result(
        self,
        decision: RiskDecision,
        signal: Signal,
        generation: Mapping[str, Any],
        run_id: str,
        candidate_id: str,
        evaluated_at: datetime,
        started: float,
        context: Mapping[str, Any],
        fingerprint: str,
        idempotency_key: str,
        sizing: Any = None,
        *,
        proposal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        evaluation_id = uuid5(
            NAMESPACE_URL, f"risk-evaluation:{idempotency_key}")
        persist_context = self._jsonable(context)
        persist_proposal = (
            self._jsonable(proposal) if proposal is not None else None)
        saved = self.proposal_repository.save_result(
            evaluation_id=str(evaluation_id),
            idempotency_key=idempotency_key,
            decision=decision,
            run_id=run_id,
            candidate_id=candidate_id,
            signal_id=str(signal.signal_id),
            generation_id=generation["generation_id"],
            evaluated_at=evaluated_at,
            input_fingerprint=fingerprint,
            context=persist_context,
            sizing=sizing,
            proposal=persist_proposal,
        )
        result = saved["payload"]
        result.setdefault("duplicate", False)
        result["duration_seconds"] = perf_counter() - started
        logger.info(
            "persisted risk evaluation",
            extra={
                "run_id": run_id,
                "signal_id": str(signal.signal_id),
                "proposal_id": result.get("proposal_id"),
                "instrument_id": signal.instrument_id,
                "strategy": signal.strategy,
                "side": signal.side.value,
                "quantity": getattr(sizing, "quantity", None),
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_loss,
                "risk_amount": (
                    proposal.get("risk_amount") if proposal is not None else None),
                "exposure": (
                    proposal.get("position_exposure") if proposal is not None else None),
                "decision": decision.status.value,
                "rejection_reason": decision.reason
                if decision.status is RiskDecisionStatus.REJECTED else None,
                "duration_seconds": result["duration_seconds"],
            },
        )
        return result

    @staticmethod
    def _stored_result(
        evaluation: Mapping[str, Any],
        proposal: Mapping[str, Any] | None,
        *,
        duplicate: bool,
        started: float,
    ) -> dict[str, Any]:
        result = dict(evaluation["payload"])
        result["trade_proposal"] = (
            proposal["payload"] if proposal is not None else result.get("trade_proposal"))
        result["duplicate"] = duplicate
        result["duration_seconds"] = perf_counter() - started
        return result

    @staticmethod
    def _stable_decision(decision: RiskDecision, key: str) -> RiskDecision:
        return replace(
            decision,
            decision_id=uuid5(NAMESPACE_URL, f"risk-decision:{key}"),
            order_id=None,
        )

    @staticmethod
    def _signal_from_row(row: Mapping[str, Any]) -> Signal:
        payload = TradeProposalService._payload_mapping(row.get("payload"))
        return Signal(
            instrument_id=row["instrument_id"],
            symbol=row["symbol"],
            timestamp=TradeProposalService._parse_time(
                row["timestamp"], "signal timestamp"),
            strategy=row["strategy"],
            side=SignalSide(row["side"]),
            entry_price=payload.get("entry_price"),
            stop_loss=payload.get("stop_loss"),
            take_profit=payload.get("take_profit"),
            reward_risk=payload.get("reward_risk"),
            confidence=row["confidence"],
            expected_edge=payload.get("expected_edge", 0.0),
            regime=payload.get("regime", "UNKNOWN"),
            reasons=tuple(payload.get("reasons", ())),
            invalidation_conditions=tuple(
                payload.get("invalidation_conditions", ())),
            signal_id=UUID(row["signal_id"]),
        )

    @staticmethod
    def _payload_mapping(value: Any) -> Mapping[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "persisted signal payload is malformed") from exc
        if not isinstance(value, Mapping):
            raise ValueError("persisted signal payload is malformed")
        return value

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _parse_time(value: Any, name: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} is invalid") from exc
        TradeProposalService._require_aware(parsed, name)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        canonical = json.dumps(
            TradeProposalService._jsonable(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, timedelta):
            return value.total_seconds()
        if is_dataclass(value) and not isinstance(value, type):
            return {
                item.name: TradeProposalService._jsonable(
                    getattr(value, item.name))
                for item in fields(value)
            }
        if isinstance(value, Mapping):
            return {
                str(key): TradeProposalService._jsonable(item)
                for key, item in value.items()
            }
        if isinstance(value, (set, frozenset)):
            normalized = [TradeProposalService._jsonable(
                item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":"), allow_nan=False),
            )
        if isinstance(value, (tuple, list)):
            return [TradeProposalService._jsonable(item) for item in value]
        return value

    def _now(self) -> datetime:
        value = self.clock()
        self._require_aware(value, "clock")
        return value.astimezone(timezone.utc)
