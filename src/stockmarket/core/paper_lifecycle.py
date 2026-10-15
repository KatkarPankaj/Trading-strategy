"""Durable proposal submission and quote-triggered paper position exits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from threading import Lock
from math import isfinite
from time import monotonic as system_monotonic
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .audit_trail import AuditContext
from .brokers import BrokerError
from .executors import TradingMode
from .models import (
    Instrument,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    Signal,
    SignalSide,
    TradeIntent,
)
from .risk import OrderIntent
from .order_management import InvalidOrderTransition, UnknownOrder
from .trade_proposals import TradeProposalService
from .trading_service import AutomaticSizingRejected, OrderTicket, PreparedQuote, TradingService

QuoteSource = Callable[[str], tuple[float, datetime] | None]
PriceUpdate = Callable[[str, float, datetime], None]

_SUBMISSION_OUTCOME = {
    OrderStatus.REJECTED: "PAPER_ORDER_REJECTED",
    OrderStatus.FAILED: "PAPER_ORDER_FAILED",
}


class PaperLifecycleError(ValueError):
    """A paper lifecycle request cannot be safely evaluated or submitted."""

    def __init__(self, message: str, *, code: str = "PAPER_LIFECYCLE_REJECTED",
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class _MonitoringBudget:
    def __init__(self, max_items: int | None, deadline: float | None,
                 monotonic: Callable[[], float] | None) -> None:
        if max_items is not None and (
                isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1):
            raise ValueError("monitoring max_items must be positive")
        if deadline is not None and not isfinite(deadline):
            raise ValueError("monitoring deadline must be finite")
        self.remaining = max_items
        self.max_quotes = max_items
        self.deadline = deadline
        self.monotonic = monotonic or system_monotonic
        self.incomplete = False
        self.allowed: set[tuple[str, str]] | None = None
        self.known: set[tuple[str, str]] = set()

    def select(self, kind: str, identifier: str) -> bool:
        if self.allowed is not None and (kind, identifier) not in self.allowed:
            if (kind, identifier) not in self.known and self.remaining is not None and self.remaining > 0:
                self.allowed.add((kind, identifier))
                return True
            self.incomplete = True
            return False
        return True

    def admit(self) -> bool:
        if self.remaining == 0 or self.deadline is not None and self.monotonic() >= self.deadline:
            self.incomplete = True
            return False
        if self.remaining is not None:
            self.remaining -= 1
        return True


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
        quote_provider: str = "unknown",
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
        self.quote_provider = quote_provider
        self.strategy_refresh: Callable[[], None] | None = None

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
        if self.strategy_refresh is not None:
            self.strategy_refresh()
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
                # Automatic sizing persists the sized quantity at finish(); the retry
                # must still request automatic sizing rather than a manual quantity.
                quantity_matches = (
                    quantity is None if sizing_mode == "AUTOMATIC_SIZING"
                    else prior["quantity"] == quantity)
                if (
                    prior["operator"] != operator
                    or prior["sizing_mode"] != sizing_mode
                    or not quantity_matches
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
            raise PaperLifecycleError("proposal is stale and must be regenerated", code="PROPOSAL_STALE")
        if not self.markets.is_regular_session(instrument.market, now):
            raise PaperLifecycleError(
                f"market session is not regular for {instrument.market}", code="MARKET_SESSION_NOT_REGULAR")
        if self.trading.portfolio.positions().get(instrument.instrument_id):
            raise PaperLifecycleError("position already exists for proposal instrument", code="POSITION_EXISTS")
        if any(
            order.instrument_id == instrument.instrument_id
            and not (retry and order.client_order_id == client_order_id)
            for order in self.trading.order_manager.open_orders()
        ):
            raise PaperLifecycleError("an open order already targets the instrument", code="OPEN_ORDER_EXISTS")

        quote = self.quotes(instrument.instrument_id)
        if quote is None:
            raise PaperLifecycleError("fresh market quote is unavailable", code="EXECUTION_QUOTE_MISSING")
        price, timestamp = quote
        quote_trace = {
            "price": price if isinstance(price, (int, float)) and not isinstance(price, bool)
            and isfinite(price) else None, "source_timestamp": timestamp.isoformat()
            if isinstance(timestamp, datetime) else None,
            "checked_at": now.isoformat(), "source": self.quote_provider,
            "instrument_id": instrument.instrument_id,
        }
        try:
            _validate_quote(instrument, price, timestamp, now, self.max_age)
        except PaperLifecycleError as exc:
            exc.details["execution_quote"] = quote_trace
            raise
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
            if sizing_mode == "AUTOMATIC_SIZING" and retry and prior["quantity"] is not None:
                # A safe retry re-sends the identical order; risk is still re-evaluated.
                submitted_quantity = int(prior["quantity"])
                result = self.trading.submit_signal(
                    signal,
                    submitted_quantity,
                    actor=f"api:{operator}",
                    audit=audit,
                    client_order_id=client_order_id,
                )
            elif sizing_mode == "AUTOMATIC_SIZING":
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
            raise PaperLifecycleError(str(exc), code=str(exc).split(":", 1)[0]) from exc
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
            "execution_quote": quote_trace,
            "duplicate": result.duplicate,
            "execution": _SUBMISSION_OUTCOME.get(
                result.order.status, "PAPER_ORDER_CREATED"),
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
            raise PaperLifecycleError("proposal strategy version is not currently registered",
                                      code="STRATEGY_VERSION_MISMATCH")
        try:
            signal = TradeProposalService._signal_from_row(signal_row)
        except ValueError as exc:
            raise PaperLifecycleError(
                f"AMBIGUOUS_LEGACY_INTENT: persisted signal cannot be submitted: {exc}") from exc
        if signal.side not in (SignalSide.BUY, SignalSide.SELL) or not signal.intent.is_entry:
            raise PaperLifecycleError("only persisted actionable entry signals may be submitted",
                                      code="NON_ACTIONABLE_SIGNAL")
        if proposal.get("intent") not in (None, signal.intent.value):
            raise PaperLifecycleError("proposal trade intent does not match its signal")
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
        self._monitor_cursor = 0
        self._flight = Lock()

    def _monitoring_outcome(self, result: dict[str, Any]) -> dict[str, Any]:
        failed = result.get("incomplete") or any(
            row.get("status") in {"MONITOR_FAILED", "RECONCILE_FAILED", "FLATTEN_FAILED",
                                  "REJECTED", "FAILED", "ENTRY_ORDER_PENDING"}
            for row in (*result.get("orders", []), *result.get("positions", [])))
        self.trading.monitoring_result("PROTECTIVE_RECONCILIATION_INCOMPLETE" if failed else None)
        if failed:
            self.store.audit.append(
                "system:paper-position-manager", "PAPER_MONITORING_INCOMPLETE", "system",
                "paper", {"at": self._now(), "error": result.get("error")})
        return result

    def _prepare_monitoring(
        self, budget: _MonitoringBudget, boundary: Any, market: str | None = None,
    ) -> tuple[tuple[Any, ...], Mapping[str, PreparedQuote | Exception]]:
        with boundary():
            self.trading.monitoring_result("PROTECTIVE_RECONCILIATION_IN_PROGRESS")
            self._plan_monitoring(budget, market)
            state = self.trading.monitoring_state()
            ids = {
                o.instrument_id for o in self.trading.order_manager.open_orders()
                if budget.select("order", o.client_order_id)
            }
            for p in self.trading.portfolio.positions().values():
                if not budget.select("position", p.instrument_id):
                    continue
                instrument = self.instruments.get(p.instrument_id)
                if instrument is None or not self.markets.is_regular_session(instrument.market, self._now()):
                    continue
                try:
                    entry = self._entry_proposal(p)
                except (PaperLifecycleError, InvalidOrderTransition, BrokerError):
                    # Authoritative evaluation records this failure without skipping other positions.
                    ids.add(p.instrument_id)
                    continue
                if entry["order"].get("status") in {
                    OrderStatus.FILLED.value, OrderStatus.CANCELLED.value,
                    OrderStatus.REJECTED.value, OrderStatus.FAILED.value,
                }:
                    ids.add(p.instrument_id)
            # Final risk also needs reservation prices for non-selected pending orders.
            reservation_ids = {o.instrument_id for o in self.trading.order_manager.open_orders()
                               if o.limit_price is None}
            quote_ids = sorted(ids) + sorted(reservation_ids - ids)
        prepared: dict[str, PreparedQuote | Exception] = {}
        for index, iid in enumerate(quote_ids):
            if budget.max_quotes is not None and index >= budget.max_quotes:
                budget.incomplete = True
                break
            instrument = self.instruments.get(iid)
            if instrument is None or market is not None and instrument.market != market \
                    or not self.markets.is_regular_session(instrument.market, self._now()):
                continue
            if budget.deadline is not None and budget.monotonic() >= budget.deadline:
                budget.incomplete = True
                break
            try:
                quote = self.quotes(iid)
                if quote is None:
                    raise PaperLifecycleError(f"fresh market quote is unavailable for {iid}")
                price, timestamp = quote
                at = self._now()
                timestamp = _validate_quote(instrument, price, timestamp, at, self.max_age)
                prepared[iid] = PreparedQuote(iid, price, timestamp, at, type(self.quotes).__name__)
            except Exception as exc:
                # Preserve per-instrument failure for the authoritative pass; no synthetic price.
                prepared[iid] = PaperLifecycleError(f"{type(exc).__name__}: {exc}") \
                    if not isinstance(exc, PaperLifecycleError) else exc
        return state, MappingProxyType(prepared)

    def _quote(self, iid: str, prepared: Mapping[str, PreparedQuote | Exception]) -> tuple[float, datetime]:
        value = prepared.get(iid)
        if isinstance(value, Exception):
            raise value
        if value is None or value.instrument_id != iid:
            raise PaperLifecycleError(f"prepared quote is unavailable for {iid}")
        _validate_quote(self.instruments[iid], value.price, value.timestamp, self._now(), self.max_age)
        if value.fetched_at > self._now():
            raise PaperLifecycleError("prepared quote is from the future")
        return value.price, value.timestamp

    def _plan_monitoring(self, budget: _MonitoringBudget, market: str | None = None) -> None:
        if budget.remaining is None:
            return
        def included(instrument_id: str) -> bool:
            instrument = self.instruments.get(instrument_id)
            return market is None or instrument is None or instrument.market == market

        items = [
            ("order", order.client_order_id) for order in self.trading.order_manager.open_orders()
            if included(order.instrument_id)]
        items.extend(
            ("position", position.instrument_id) for position in self.trading.portfolio.positions().values()
            if included(position.instrument_id))
        budget.known = set(items)
        if not items:
            budget.allowed = set()
            return
        offset = self._monitor_cursor % len(items)
        count = min(budget.remaining, len(items))
        budget.allowed = {items[(offset + n) % len(items)] for n in range(count)}
        budget.incomplete = count < len(items)
        self._monitor_cursor = (offset + count) % len(items)

    def manage(
        self, *, max_items: int | None = None, deadline: float | None = None,
        monotonic: Callable[[], float] | None = None, lock_timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self._flight.acquire(blocking=False):
            return {"incomplete": True, "orders": [], "positions": [], "error": "MONITORING_BUSY"}
        try:
            return self._manage(max_items=max_items, deadline=deadline, monotonic=monotonic,
                                lock_timeout=lock_timeout)
        finally:
            self._flight.release()

    def _manage(
        self, *, max_items: int | None, deadline: float | None,
        monotonic: Callable[[], float] | None, lock_timeout: float | None,
    ) -> dict[str, Any]:
        budget = _MonitoringBudget(max_items, deadline, monotonic)
        boundary = (self.trading.paper_strategy_change if lock_timeout is None
                    else lambda: self.trading.paper_monitoring(lock_timeout))
        state, prepared = self._prepare_monitoring(budget, boundary)
        with boundary():
            if state != self.trading.monitoring_state():
                return self._monitoring_outcome({
                    "trading_mode": TradingMode.PAPER.value, "orders": [], "positions": [],
                    "incomplete": True, "error": "MONITORING_STATE_CHANGED"})
            orders = self._pending_orders(budget=budget, prepared=prepared)
            evaluated = []
            for position in list(self.trading.portfolio.positions().values()):
                if not budget.select("position", position.instrument_id):
                    continue
                if not budget.admit():
                    break
                try:
                    evaluated.extend(self._manage_position(position, prepared)["positions"])
                except (PaperLifecycleError, InvalidOrderTransition, BrokerError) as exc:
                    self.store.audit.append(
                        "system:paper-position-manager", "PAPER_POSITION_MONITOR_FAILED",
                        "instrument", position.instrument_id, {"error": str(exc), "at": self._now()})
                    evaluated.append({"instrument_id": position.instrument_id,
                                      "status": "MONITOR_FAILED", "error": str(exc)})
            return self._monitoring_outcome({
                "trading_mode": TradingMode.PAPER.value, "orders": orders, "positions": evaluated,
                "incomplete": budget.incomplete})

    def _pending_orders(self, *, cancel_entries: bool = False,
                        market: str | None = None,
                        budget: _MonitoringBudget | None = None,
                        prepared: Mapping[str, PreparedQuote | Exception]) -> list[dict[str, Any]]:
        results = []
        now = self._now()
        for order in self.trading.order_manager.open_orders():
            instrument = self.instruments.get(order.instrument_id)
            if instrument is not None and market is not None and instrument.market != market:
                continue
            if budget is not None and not budget.select("order", order.client_order_id):
                continue
            if budget is not None and not budget.admit():
                break
            try:
                if instrument is None:
                    raise PaperLifecycleError("pending order references unknown instrument")
                if cancel_entries and order.trade_intent in {TradeIntent.BUY, TradeIntent.SHORT_SELL}:
                    current = self.trading.cancel(
                        order.client_order_id, actor="system:paper-position-manager")
                elif self.markets.is_regular_session(instrument.market, now):
                    price, timestamp = self._quote(instrument.instrument_id, prepared)
                    _validate_quote(instrument, price, timestamp, now, self.max_age)
                    self.update_price(instrument.instrument_id, price, timestamp)
                    current = self.trading.order_manager.sync(order.client_order_id)
                else:
                    current = self.trading.order_manager.sync(order.client_order_id)
                results.append({"client_order_id": order.client_order_id,
                                "instrument_id": order.instrument_id, "status": current.status.value})
            except (PaperLifecycleError, InvalidOrderTransition, BrokerError) as exc:
                self.store.audit.append(
                    "system:paper-position-manager", "PAPER_ORDER_RECONCILE_FAILED",
                    "order", order.client_order_id, {"error": str(exc), "at": now})
                results.append({"client_order_id": order.client_order_id,
                                "instrument_id": order.instrument_id,
                                "status": "RECONCILE_FAILED", "error": str(exc)})
        return results

    def _manage_position(self, monitored_position: Any,
                         prepared: Mapping[str, PreparedQuote | Exception]) -> dict[str, Any]:
        now = self._now()
        position = monitored_position
        instrument = self.instruments.get(position.instrument_id)
        if instrument is None:
            raise PaperLifecycleError(
                f"open position references unknown instrument {position.instrument_id}")
        if not self.markets.is_regular_session(instrument.market, now):
            return {"positions": [{"instrument_id": instrument.instrument_id, "status": "SESSION_CLOSED"}]}
        entry = self._entry_proposal(position)
        try:
            entry_status = OrderStatus(entry["order"]["status"])
        except (KeyError, ValueError) as exc:
            raise PaperLifecycleError(
                f"entry order state is unavailable for {instrument.instrument_id}") from exc
        if entry_status not in {
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FAILED,
        }:
            return {"positions": [{"instrument_id": instrument.instrument_id,
                                   "status": "ENTRY_ORDER_PENDING",
                                   "client_order_id": entry["client_order_id"]}]}
        proposal = entry["proposal"]
        stop, target = proposal.get("stop_price"), proposal.get("target_price")
        if stop is None:
            raise PaperLifecycleError(
                f"position {instrument.instrument_id} has no persisted protective stop")
        price, timestamp = self._quote(instrument.instrument_id, prepared)
        timestamp = _validate_quote(instrument, price, timestamp, now, self.max_age)
        if timestamp < position.opened_at:
            raise PaperLifecycleError(f"quote predates the open position for {instrument.instrument_id}")
        if not instrument.is_valid_price(stop) \
                or target is not None and not instrument.is_valid_price(target):
            raise PaperLifecycleError(f"persisted exit levels are invalid for {instrument.instrument_id}")
        if (position.side is PositionSide.LONG and (
                stop >= position.average_entry_price or target is not None and target <= position.average_entry_price)) \
                or (position.side is PositionSide.SHORT and (
                    stop <= position.average_entry_price or target is not None and target >= position.average_entry_price)):
            raise PaperLifecycleError(
                f"persisted exit levels contradict position direction for {instrument.instrument_id}")
        self.update_price(instrument.instrument_id, price, timestamp)
        position = self.trading.portfolio.positions().get(instrument.instrument_id)
        if position is None:
            self._record_evaluation(
                position=None, entry=entry, timestamp=timestamp, status="CLOSED_DURING_MARKET_UPDATE",
                reason=None, price=price, stop=stop, target=target, instrument_id=instrument.instrument_id)
            return {"positions": [{"instrument_id": instrument.instrument_id,
                                   "status": "CLOSED_DURING_MARKET_UPDATE",
                                   "quote_price": price, "quote_timestamp": timestamp}]}
        reason = _exit_trigger(position.side, price, stop, target)
        if reason is None:
            self._record_evaluation(
                position, entry, timestamp, "MONITORED", None, price=price, stop=stop, target=target)
            return {"positions": [{"instrument_id": instrument.instrument_id, "status": "MONITORED",
                                   "quote_price": price, "quote_timestamp": timestamp}]}
        outcome = self._submit_exit(
            position=position, instrument=instrument, entry=entry,
            price=price, timestamp=timestamp, reason=reason, prepared=prepared)
        self._record_evaluation(
            position, entry, timestamp, str(outcome["status"]), reason,
            price=price, stop=stop, target=target,
            exit_proposal_id=outcome.get("proposal_id"), exit_client_order_id=outcome.get("client_order_id"))
        return {"positions": [outcome]}

    def flatten(
        self, *, reason: str = "END_OF_DAY_FLATTEN", market: str | None = None,
        max_items: int | None = None, deadline: float | None = None,
        monotonic: Callable[[], float] | None = None, lock_timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self._flight.acquire(blocking=False):
            return {"incomplete": True, "orders": [], "positions": [], "error": "MONITORING_BUSY"}
        try:
            return self._flatten_cycle(reason=reason, market=market, max_items=max_items,
                                       deadline=deadline, monotonic=monotonic, lock_timeout=lock_timeout)
        finally:
            self._flight.release()

    def _flatten_cycle(
        self, *, reason: str, market: str | None, max_items: int | None,
        deadline: float | None, monotonic: Callable[[], float] | None, lock_timeout: float | None,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("flatten reason is required")
        budget = _MonitoringBudget(max_items, deadline, monotonic)
        boundary = (self.trading.paper_strategy_change if lock_timeout is None
                    else lambda: self.trading.paper_monitoring(lock_timeout))
        state, prepared = self._prepare_monitoring(budget, boundary, market)
        with boundary():
            if state != self.trading.monitoring_state():
                return self._monitoring_outcome({
                    "trading_mode": TradingMode.PAPER.value, "orders": [], "positions": [],
                    "incomplete": True, "error": "MONITORING_STATE_CHANGED"})
            orders = self._pending_orders(cancel_entries=True, market=market, budget=budget, prepared=prepared)
            result = self._flatten(reason=reason, market=market, budget=budget, prepared=prepared)
            return self._monitoring_outcome({**result, "orders": orders, "incomplete": budget.incomplete})

    def _flatten(
        self, *, reason: str, market: str | None, budget: _MonitoringBudget,
        prepared: Mapping[str, PreparedQuote | Exception],
    ) -> dict[str, Any]:
        """Close every open paper position (optionally one market) with a fresh in-session quote.

        Exits go through the ordinary RiskEngine/OrderManager path with an explicit close intent.
        A failure for one position is reported and does not stop the others.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("flatten reason is required")
        now = self._now()
        results: list[dict[str, Any]] = []
        for position in list(self.trading.portfolio.positions().values()):
            instrument = self.instruments.get(position.instrument_id)
            if instrument is not None and market is not None and instrument.market != market:
                continue
            if not budget.select("position", position.instrument_id):
                continue
            if not budget.admit():
                break
            if instrument is None:
                results.append({"instrument_id": position.instrument_id,
                                "status": "FLATTEN_FAILED",
                                "error": "open position references unknown instrument"})
                continue
            try:
                if not self.markets.is_regular_session(instrument.market, now):
                    results.append({"instrument_id": instrument.instrument_id,
                                    "status": "SESSION_CLOSED"})
                    continue
                entry = self._entry_proposal(position)
                if entry["order"].get("status") not in {
                    OrderStatus.FILLED.value, OrderStatus.CANCELLED.value,
                    OrderStatus.REJECTED.value, OrderStatus.FAILED.value,
                }:
                    results.append({"instrument_id": instrument.instrument_id,
                                    "status": "ENTRY_ORDER_PENDING",
                                    "client_order_id": entry["client_order_id"]})
                    continue
                price, timestamp = self._quote(instrument.instrument_id, prepared)
                timestamp = _validate_quote(instrument, price, timestamp, now, self.max_age)
                self.update_price(instrument.instrument_id, price, timestamp)
                current = self.trading.portfolio.positions().get(instrument.instrument_id)
                if current is None:
                    results.append({"instrument_id": instrument.instrument_id,
                                    "status": "CLOSED_DURING_MARKET_UPDATE"})
                    continue
                outcome = self._submit_exit(
                    position=current, instrument=instrument, entry=entry,
                    price=price, timestamp=timestamp, reason=reason, prepared=prepared)
                proposal = entry["proposal"]
                self._record_evaluation(
                    current, entry, timestamp, str(outcome["status"]), reason,
                    price=price, stop=proposal.get("stop_price"),
                    target=proposal.get("target_price"),
                    exit_proposal_id=outcome.get("proposal_id"),
                    exit_client_order_id=outcome.get("client_order_id"),
                )
                results.append(outcome)
            except (PaperLifecycleError, InvalidOrderTransition, BrokerError) as exc:
                self.store.audit.append(
                    "system:paper-position-manager", "PAPER_POSITION_FLATTEN_FAILED",
                    "instrument", instrument.instrument_id, {"error": str(exc), "at": now})
                results.append({"instrument_id": instrument.instrument_id,
                                "status": "FLATTEN_FAILED", "error": str(exc)})
        return {"trading_mode": TradingMode.PAPER.value, "reason": reason, "positions": results}

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
        prepared: Mapping[str, PreparedQuote | Exception],
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
        trade_intent = TradeIntent.close_for(position.side)
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
            intent=trade_intent,
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
                "trade_intent": trade_intent.value,
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
            trade_intent=trade_intent,
            audit=AuditContext(
                exit_reason=reason,
                data_reference=f"position-exit:{proposal_id}",
            ),
            signal=signal,
        )
        result = self.trading.submit(
            ticket, actor="system:paper-position-manager",
            exit_quotes={iid: q for iid, q in prepared.items() if isinstance(q, PreparedQuote)})
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
        for fill in self.store.fills.for_position(position.instrument_id, position.opened_at):
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
            f"quote price is invalid or off tick for {instrument.instrument_id}", code="EXECUTION_QUOTE_INVALID")
    quote_at = _parse_aware(timestamp, "quote timestamp")
    age = now - quote_at
    if age < timedelta(0) or age > max_age:
        raise PaperLifecycleError(
            f"quote is stale or from the future for {instrument.instrument_id}",
            code="EXECUTION_QUOTE_FUTURE" if age < timedelta(0) else "EXECUTION_QUOTE_STALE")
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
