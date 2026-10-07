"""Durable proposal submission and quote-triggered paper position exits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .audit_trail import AuditContext
from .executors import TradingMode
from .models import (
    Instrument,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    Signal,
    SignalSide,
)
from .risk import OrderIntent
from .order_management import UnknownOrder
from .trade_proposals import TradeProposalService
from .trading_service import AutomaticSizingRejected, OrderTicket, TradingService

QuoteSource = Callable[[str], tuple[float, datetime] | None]
PriceUpdate = Callable[[str, float, datetime], None]


class PaperLifecycleError(ValueError):
    """A paper lifecycle request cannot be safely evaluated or submitted."""


class PaperProposalExecutionService:
    """Submit only persisted 2C-3 approvals, through TradingService and OrderManager."""

    def __init__(
        self,
        *,
        store: Any,
        trading: TradingService,
        instruments: Mapping[str, Instrument],
        markets: Any,
        quotes: QuoteSource,
        update_price: PriceUpdate,
        strategies: Mapping[str, Any],
        max_age: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if trading.mode is not TradingMode.PAPER:
            raise ValueError("proposal execution requires PAPER mode")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self.store = store
        self.trading = trading
        self.instruments = dict(instruments)
        self.markets = markets
        self.quotes = quotes
        self.update_price = update_price
        self.strategies = dict(strategies)
        self.max_age = max_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(
        self,
        proposal_id: str,
        *,
        operator: str,
        sizing_mode: str,
        quantity: int | None,
    ) -> dict[str, Any]:
        if self.trading.mode is not TradingMode.PAPER:
            raise PaperLifecycleError("proposal submission is restricted to PAPER mode")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise PaperLifecycleError("proposal_id must be non-empty")
        if not isinstance(operator, str) or not operator.strip():
            raise PaperLifecycleError("operator must be non-empty")
        if sizing_mode not in ("AUTOMATIC_SIZING", "MANUAL_OVERRIDE"):
            raise PaperLifecycleError("unsupported sizing mode")
        if sizing_mode == "AUTOMATIC_SIZING" and quantity is not None:
            raise PaperLifecycleError("automatic sizing does not accept quantity")
        if sizing_mode == "MANUAL_OVERRIDE" and (
            isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
        ):
            raise PaperLifecycleError("manual override requires a positive integer quantity")

        client_order_id = _proposal_order_id(proposal_id)
        prior = self.store.proposal_submissions.get(proposal_id)
        retry = False
        if prior is not None:
            if prior["client_order_id"] != client_order_id:
                raise PaperLifecycleError(
                    "persisted proposal order identity does not match deterministic id")
            if str(prior["state"]).startswith("ORDER_"):
                try:
                    order = self.trading.order_manager.get(client_order_id)
                except UnknownOrder as exc:
                    raise PaperLifecycleError(
                        "proposal submission exists but order recovery is incomplete") from exc
                return {
                    "proposal_id": proposal_id,
                    "client_order_id": client_order_id,
                    "order": order,
                    "duplicate": True,
                    "execution": "PAPER_ORDER_ALREADY_CREATED",
                }
            if prior["state"] == "REJECTED_BEFORE_ORDER":
                raise PaperLifecycleError(
                    f"proposal was rejected before order creation: {prior['error']}")
            if prior["state"] == "RETRYABLE":
                if (
                    prior["operator"] != operator
                    or prior["sizing_mode"] != sizing_mode
                    or prior["quantity"] != quantity
                ):
                    raise PaperLifecycleError(
                        "safe retry must use the original operator and sizing parameters")
                retry = True
            else:
                raise PaperLifecycleError(
                    "proposal submission is already claimed; reconcile before retrying")

        record = self.store.trade_proposals.get_by_id(proposal_id)
        if record is None:
            raise KeyError(f"unknown persisted approved proposal {proposal_id!r}")
        proposal = record["payload"]
        signal, instrument = self._validate_provenance(record, proposal)
        now = self._now()
        proposal_at = _parse_aware(proposal.get("created_at"), "proposal created_at")
        generation = self.store.signal_generations.get_by_signal(
            str(proposal.get("signal_id")))
        generated_at = _parse_aware(
            generation.get("generated_at") if generation else None,
            "signal generation timestamp")
        if (
            proposal_at > now
            or generated_at > now
            or signal.timestamp > now
            or now - proposal_at > self.max_age
            or now - generated_at > self.max_age
            or now - signal.timestamp > self.max_age
        ):
            raise PaperLifecycleError("proposal is stale and must be regenerated")
        if not self.markets.is_regular_session(instrument.market, now):
            raise PaperLifecycleError(
                f"market session is not regular for {instrument.market}")
        if self.trading.portfolio.positions().get(instrument.instrument_id):
            raise PaperLifecycleError("position already exists for proposal instrument")
        if any(
            order.instrument_id == instrument.instrument_id
            for order in self.trading.order_manager.open_orders()
        ):
            raise PaperLifecycleError("an open order already targets the instrument")

        quote = self.quotes(instrument.instrument_id)
        if quote is None:
            raise PaperLifecycleError("fresh market quote is unavailable")
        price, timestamp = quote
        _validate_quote(instrument, price, timestamp, now, self.max_age)
        self.update_price(instrument.instrument_id, price, timestamp)

        claimed = (
            self.store.proposal_submissions.retry(proposal_id)
            if retry else self.store.proposal_submissions.begin(
                proposal_id=proposal_id,
                client_order_id=client_order_id,
                operator=operator,
                sizing_mode=sizing_mode,
                quantity=quantity,
                proposal_as_of=proposal_at,
                generated_at=_parse_aware(
                    proposal.get("provenance", {}).get(
                        "evaluation_as_of", proposal_at),
                    "proposal evaluation timestamp"),
                proposal_payload=proposal,
            )
        )
        if not claimed:
            raise PaperLifecycleError(
                "proposal submission is already claimed; reconcile before retrying")

        audit = AuditContext(
            technical_signals={
                "signal_id": str(signal.signal_id),
                "strategy": signal.strategy,
                "reasons": signal.reasons,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
            },
            sizing={
                "method": sizing_mode,
                "approved_proposal_id": proposal_id,
                "approved_quantity": proposal.get("quantity"),
                "submitted_quantity": quantity,
            },
            data_reference=f"trade-proposal:{proposal_id}",
        )
        try:
            if sizing_mode == "AUTOMATIC_SIZING":
                result, sizing = self.trading.submit_sized_signal(
                    signal,
                    actor=f"api:{operator}",
                    audit=audit,
                    client_order_id=client_order_id,
                )
                submitted_quantity = sizing.quantity
            else:
                result = self.trading.submit_signal(
                    signal,
                    quantity,
                    actor=f"api:{operator}",
                    audit=audit,
                    client_order_id=client_order_id,
                )
                submitted_quantity = quantity
        except AutomaticSizingRejected as exc:
            self.store.proposal_submissions.finish(
                proposal_id,
                state="REJECTED_BEFORE_ORDER",
                quantity=None,
                error=str(exc),
            )
            raise PaperLifecycleError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            self.store.proposal_submissions.finish(
                proposal_id,
                state="REJECTED_BEFORE_ORDER",
                quantity=quantity,
                error=str(exc),
            )
            raise PaperLifecycleError(str(exc)) from exc

        self.store.proposal_submissions.finish(
            proposal_id,
            state=f"ORDER_{result.order.status.value}",
            quantity=submitted_quantity,
            error=result.order.error,
        )
        return {
            "proposal_id": proposal_id,
            "client_order_id": client_order_id,
            "order": result.order,
            "risk_decision": result.risk,
            "duplicate": result.duplicate,
            "execution": "PAPER_ORDER_CREATED",
        }

    def _validate_provenance(
        self, record: Mapping[str, Any], proposal: Mapping[str, Any],
    ) -> tuple[Signal, Instrument]:
        if record.get("evaluation_status") != "APPROVED":
            raise PaperLifecycleError("proposal has no persisted approved risk evaluation")
        if record.get("proposal_id") != proposal.get("proposal_id"):
            raise PaperLifecycleError("proposal identity does not match its stored payload")
        if proposal.get("risk_decision_id") is None:
            raise PaperLifecycleError("approved proposal is missing risk decision provenance")

        run = self.store.autonomous_research.get_run(str(proposal.get("run_id")))
        candidate = self.store.autonomous_research.get_candidate(
            str(proposal.get("run_id")), str(proposal.get("candidate_id")))
        generation = self.store.signal_generations.get_by_signal(
            str(proposal.get("signal_id")))
        signal_row = self.store.signals.get(str(proposal.get("signal_id")))
        if run is None or candidate is None or generation is None or signal_row is None:
            raise PaperLifecycleError("proposal research or signal provenance is incomplete")
        request = run["payload"].get("request", {})
        if (
            run.get("status") not in ("COMPLETE", "PARTIAL")
            or run.get("stage") != "STRATEGY_SELECTED"
            or request.get("mode") != "PAPER"
            or candidate.get("stage") != "SIGNAL_GENERATED"
            or generation.get("status") != "SIGNAL_GENERATED"
            or generation.get("run_id") != proposal.get("run_id")
            or generation.get("candidate_id") != proposal.get("candidate_id")
            or generation.get("generation_id") != record.get("generation_id")
            or generation.get("strategy_name") != proposal.get("strategy")
            or generation.get("strategy_version") != proposal.get("strategy_version")
            or generation.get("signal_id") != proposal.get("signal_id")
        ):
            raise PaperLifecycleError("proposal provenance does not match persisted PAPER research")
        instrument = self.instruments.get(str(proposal.get("instrument_id")))
        if instrument is None or (
            signal_row.get("instrument_id") != instrument.instrument_id
            or signal_row.get("symbol") != instrument.symbol
            or signal_row.get("strategy") != proposal.get("strategy")
            or signal_row.get("signal_id") != proposal.get("signal_id")
            or record.get("instrument_id") != instrument.instrument_id
            or record.get("side") != proposal.get("side")
        ):
            raise PaperLifecycleError("proposal signal and instrument identity do not match")
        strategy = self.strategies.get(str(proposal.get("strategy")))
        current_version = getattr(strategy, "version", None) if strategy else None
        if current_version is None and strategy is not None:
            current_version = getattr(getattr(strategy, "metadata", None), "version", None)
        if current_version != proposal.get("strategy_version"):
            raise PaperLifecycleError("proposal strategy version is not currently registered")
        signal = TradeProposalService._signal_from_row(signal_row)
        if signal.side not in (SignalSide.BUY, SignalSide.SELL):
            raise PaperLifecycleError("only persisted actionable signals may be submitted")
        if (
            proposal.get("signal_id") != str(signal.signal_id)
            or proposal.get("side") != signal.side.value
            or proposal.get("entry_price") != signal.entry_price
            or proposal.get("stop_price") != signal.stop_loss
            or proposal.get("target_price") != signal.take_profit
        ):
            raise PaperLifecycleError("proposal prices or side do not match its signal")
        return signal, instrument

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PaperLifecycleError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)


class PaperPositionManager:
    """Evaluate persisted entry stop/target levels and route triggered exits via RiskEngine."""

    def __init__(
        self,
        *,
        store: Any,
        trading: TradingService,
        instruments: Mapping[str, Instrument],
        markets: Any,
        quotes: QuoteSource,
        update_price: PriceUpdate,
        max_age: timedelta,
        clock: Callable[[], datetime] | None = None,
        evaluation_repository: Any = None,
    ) -> None:
        if trading.mode is not TradingMode.PAPER:
            raise ValueError("position management requires PAPER mode")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self.store = store
        self.trading = trading
        self.instruments = dict(instruments)
        self.markets = markets
        self.quotes = quotes
        self.update_price = update_price
        self.max_age = max_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evaluation_repository = evaluation_repository

    def manage(self) -> dict[str, Any]:
        now = self._now()
        evaluated: list[dict[str, Any]] = []
        for position in self.trading.portfolio.positions().values():
            instrument = self.instruments.get(position.instrument_id)
            if instrument is None:
                raise PaperLifecycleError(
                    f"open position references unknown instrument {position.instrument_id}")
            entry = self._entry_proposal(position)
            try:
                entry_status = OrderStatus(entry["order"]["status"])
            except (KeyError, ValueError) as exc:
                raise PaperLifecycleError(
                    f"entry order state is unavailable for {instrument.instrument_id}") from exc
            if entry_status not in {
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
            }:
                evaluated.append({
                    "instrument_id": instrument.instrument_id,
                    "status": "ENTRY_ORDER_PENDING",
                    "client_order_id": entry["client_order_id"],
                })
                continue
            proposal = entry["proposal"]
            stop = proposal.get("stop_price")
            target = proposal.get("target_price")
            if stop is None:
                raise PaperLifecycleError(
                    f"position {instrument.instrument_id} has no persisted protective stop")
            quote = self.quotes(instrument.instrument_id)
            if quote is None:
                raise PaperLifecycleError(
                    f"fresh market quote is unavailable for {instrument.instrument_id}")
            price, timestamp = quote
            timestamp = _validate_quote(
                instrument, price, timestamp, now, self.max_age)
            if timestamp < position.opened_at:
                raise PaperLifecycleError(
                    f"quote predates the open position for {instrument.instrument_id}")
            if not self.markets.is_regular_session(instrument.market, now):
                self._record_evaluation(
                    position, entry, timestamp, "SESSION_CLOSED", None,
                    price=price, stop=stop, target=target)
                evaluated.append({
                    "instrument_id": instrument.instrument_id,
                    "status": "SESSION_CLOSED",
                    "quote_price": price,
                    "quote_timestamp": timestamp,
                })
                continue
            if (
                not instrument.is_valid_price(stop)
                or (target is not None and not instrument.is_valid_price(target))
            ):
                raise PaperLifecycleError(
                    f"persisted exit levels are invalid for {instrument.instrument_id}")
            if (
                position.side is PositionSide.LONG
                and (stop >= position.average_entry_price
                     or (target is not None and target <= position.average_entry_price))
            ) or (
                position.side is PositionSide.SHORT
                and (stop <= position.average_entry_price
                     or (target is not None and target >= position.average_entry_price))
            ):
                raise PaperLifecycleError(
                    f"persisted exit levels contradict position direction for {instrument.instrument_id}")
            self.update_price(instrument.instrument_id, price, timestamp)
            position = self.trading.portfolio.positions().get(
                instrument.instrument_id)
            if position is None:
                self._record_evaluation(
                    position=None,
                    entry=entry,
                    timestamp=timestamp,
                    status="CLOSED_DURING_MARKET_UPDATE",
                    reason=None,
                    price=price,
                    stop=stop,
                    target=target,
                    instrument_id=instrument.instrument_id,
                )
                evaluated.append({
                    "instrument_id": instrument.instrument_id,
                    "status": "CLOSED_DURING_MARKET_UPDATE",
                    "quote_price": price,
                    "quote_timestamp": timestamp,
                })
                continue
            reason = _exit_trigger(position.side, price, stop, target)
            if reason is None:
                self._record_evaluation(
                    position, entry, timestamp, "MONITORED", None,
                    price=price, stop=stop, target=target)
                evaluated.append({
                    "instrument_id": instrument.instrument_id,
                    "status": "MONITORED",
                    "quote_price": price,
                    "quote_timestamp": timestamp,
                })
                continue
            outcome = self._submit_exit(
                position=position,
                instrument=instrument,
                entry=entry,
                price=price,
                timestamp=timestamp,
                reason=reason,
            )
            self._record_evaluation(
                position, entry, timestamp, str(outcome["status"]), reason,
                price=price, stop=stop, target=target,
                exit_proposal_id=outcome.get("proposal_id"),
                exit_client_order_id=outcome.get("client_order_id"),
            )
            evaluated.append(outcome)
        return {"trading_mode": TradingMode.PAPER.value, "positions": evaluated}

    def _record_evaluation(
        self,
        position: Any,
        entry: Mapping[str, Any],
        timestamp: datetime,
        status: str,
        reason: str | None,
        *,
        price: float,
        stop: float,
        target: float | None,
        instrument_id: str | None = None,
        **details: Any,
    ) -> None:
        if self.evaluation_repository is None:
            return
        entry_client_order_id = str(entry["client_order_id"])
        actual_instrument_id = (
            instrument_id if instrument_id is not None else position.instrument_id)
        evaluation_id = str(uuid5(
            NAMESPACE_URL,
            f"position-exit-evaluation:{entry_client_order_id}:{timestamp.isoformat()}",
        ))
        self.evaluation_repository.save(
            evaluation_id=evaluation_id,
            entry_client_order_id=entry_client_order_id,
            instrument_id=actual_instrument_id,
            quote_timestamp=timestamp,
            status=status,
            trigger_reason=reason,
            payload={
                "quote_price": price,
                "stop_price": stop,
                "target_price": target,
                "position_side": position.side.value if position is not None else None,
                "position_quantity": position.quantity if position is not None else None,
                **details,
            },
        )

    def _submit_exit(
        self,
        *,
        position: Any,
        instrument: Instrument,
        entry: Mapping[str, Any],
        price: float,
        timestamp: datetime,
        reason: str,
    ) -> dict[str, Any]:
        entry_client_id = str(entry["client_order_id"])
        for previous in self.store.position_exit_proposals.for_entry(entry_client_id):
            if previous["status"] in {
                "PROPOSED", "NEW", "VALIDATED", "SUBMITTED", "ACCEPTED",
                "PARTIALLY_FILLED", "CANCEL_PENDING",
            }:
                order = self.trading.order_manager.get(previous["client_order_id"])
                order = self.trading.order_manager.sync(previous["client_order_id"])
                self.store.position_exit_proposals.finish(
                    previous["proposal_id"],
                    status=order.status.value,
                    risk_decision_id=(
                        str(order.risk_decision_id)
                        if order.risk_decision_id is not None else None),
                    error=order.error,
                )
            if previous["status"] in {
                "PROPOSED", "NEW", "VALIDATED", "SUBMITTED", "ACCEPTED",
                "PARTIALLY_FILLED", "CANCEL_PENDING",
            }:
                updated = self.trading.order_manager.get(previous["client_order_id"])
                if updated.status.value in {
                    "NEW", "VALIDATED", "SUBMITTED", "ACCEPTED",
                    "PARTIALLY_FILLED", "CANCEL_PENDING",
                }:
                    return {
                        "instrument_id": instrument.instrument_id,
                        "status": "EXIT_PENDING",
                        "client_order_id": previous["client_order_id"],
                        "proposal_id": previous["proposal_id"],
                    }

        proposal_id = str(uuid5(
            NAMESPACE_URL,
            f"paper-exit-proposal:{entry_client_id}:{reason}:{timestamp.isoformat()}",
        ))
        client_order_id = str(uuid5(NAMESPACE_URL, f"paper-exit-order:{proposal_id}"))
        side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
        signal_side = SignalSide(side.value)
        signal_id = uuid5(NAMESPACE_URL, f"paper-exit-signal:{proposal_id}")
        signal = Signal(
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            timestamp=timestamp,
            strategy=str(entry["order"]["strategy"]),
            side=signal_side,
            entry_price=price,
            reasons=(reason,),
            signal_id=signal_id,
        )
        self.store.position_exit_proposals.prepare(
            proposal_id=proposal_id,
            instrument_id=instrument.instrument_id,
            entry_client_order_id=entry_client_id,
            trigger_reason=reason,
            side=side.value,
            quantity=position.quantity,
            quote_price=price,
            quote_timestamp=timestamp,
            client_order_id=client_order_id,
            payload={
                "position_side": position.side.value,
                "position_quantity": position.quantity,
                "position_average_entry_price": position.average_entry_price,
                "proposal_id": entry["proposal"].get("proposal_id"),
                "signal_id": str(signal_id),
                "execution": "PAPER",
            },
        )
        ticket = OrderTicket(
            instrument_id=instrument.instrument_id,
            side=side,
            quantity=position.quantity,
            order_type=OrderType.MARKET,
            strategy=signal.strategy,
            client_order_id=client_order_id,
            signal_id=signal_id,
            intent=OrderIntent.EXIT,
            audit=AuditContext(
                exit_reason=reason,
                data_reference=f"position-exit:{proposal_id}",
            ),
            signal=signal,
        )
        result = self.trading.submit(ticket, actor="system:paper-position-manager")
        self.store.position_exit_proposals.finish(
            proposal_id,
            status=result.order.status.value,
            risk_decision_id=str(result.risk.decision_id),
            error=result.order.error,
        )
        return {
            "instrument_id": instrument.instrument_id,
            "status": result.order.status.value,
            "proposal_id": proposal_id,
            "client_order_id": result.order.client_order_id,
            "trigger_reason": reason,
            "risk_decision": result.risk,
            "order": result.order,
        }

    def _entry_proposal(self, position: Any) -> dict[str, Any]:
        for fill in self.store.fills.all():
            client_order_id = fill.get("client_order_id")
            if not client_order_id or fill["instrument_id"] != position.instrument_id:
                continue
            order = self.store.orders.get(client_order_id)
            if order is None or order["side"] != (
                OrderSide.BUY.value if position.side is PositionSide.LONG
                else OrderSide.SELL.value
            ):
                continue
            fill_at = _parse_aware(fill["timestamp"], "fill timestamp")
            if fill_at < position.opened_at:
                continue
            submission = self.store.proposal_submissions.for_order(client_order_id)
            if submission is None:
                continue
            proposal_id = submission["proposal_id"]
            record = self.store.trade_proposals.get_by_id(proposal_id)
            if record is not None and record["evaluation_status"] == "APPROVED":
                return {
                    "client_order_id": client_order_id,
                    "order": order,
                    "proposal": record["payload"],
                }
        raise PaperLifecycleError(
            f"position {position.instrument_id} has no persisted approved proposal provenance")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PaperLifecycleError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)


def _proposal_order_id(proposal_id: str) -> str:
    return "proposal-" + uuid5(NAMESPACE_URL, f"paper-order:{proposal_id}").hex


def _parse_aware(value: Any, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PaperLifecycleError(f"{name} is invalid") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperLifecycleError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_quote(
    instrument: Instrument,
    price: Any,
    timestamp: Any,
    now: datetime,
    max_age: timedelta,
) -> datetime:
    if (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not isfinite(price)
        or not instrument.is_valid_price(price)
    ):
        raise PaperLifecycleError(
            f"quote price is invalid or off tick for {instrument.instrument_id}")
    quote_at = _parse_aware(timestamp, "quote timestamp")
    age = now - quote_at
    if age < timedelta(0) or age > max_age:
        raise PaperLifecycleError(
            f"quote is stale or from the future for {instrument.instrument_id}")
    return quote_at


def _exit_trigger(
    side: PositionSide,
    price: float,
    stop: float,
    target: float | None,
) -> str | None:
    if side is PositionSide.LONG:
        if price <= stop:
            return "STOP_LOSS"
        if target is not None and price >= target:
            return "TAKE_PROFIT"
    else:
        if price >= stop:
            return "STOP_LOSS"
        if target is not None and price <= target:
            return "TAKE_PROFIT"
    return None
