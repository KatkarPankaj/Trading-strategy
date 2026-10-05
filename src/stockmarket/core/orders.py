"""In-memory order lifecycle management for paper execution only."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Mapping
from uuid import UUID

from .execution import PaperExecutor, PaperFill, PaperPortfolio
from .models import (
    Instrument,
    Order,
    OrderStatus,
    RiskDecision,
    RiskDecisionStatus,
)
from .risk import OrderIntent, RiskContext, RiskEngine


class InvalidOrderTransition(ValueError):
    """Raised when an order is moved across an invalid lifecycle edge."""


@dataclass(frozen=True, slots=True)
class OrderSubmissionResult:
    order: Order
    risk_decision: RiskDecision
    fill: PaperFill | None = None
    error: str | None = None
    duplicate: bool = False

    @property
    def status(self) -> OrderStatus:
        return self.order.status

    @property
    def approved(self) -> bool:
        return self.risk_decision.status is RiskDecisionStatus.APPROVED


_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset(
        {OrderStatus.VALIDATED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.VALIDATED: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED}
    ),
    OrderStatus.ACCEPTED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
         OrderStatus.CANCEL_PENDING, OrderStatus.FAILED}
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
         OrderStatus.CANCEL_PENDING, OrderStatus.FAILED}
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {OrderStatus.CANCELLED, OrderStatus.PARTIALLY_FILLED,
         OrderStatus.FILLED, OrderStatus.FAILED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


class OrderManager:
    """Risk-gate and track order lifecycle before invoking the paper executor."""

    def __init__(self, risk_engine: RiskEngine, paper_executor: PaperExecutor) -> None:
        if not isinstance(risk_engine, RiskEngine):
            raise TypeError("risk_engine must be a RiskEngine")
        if not isinstance(paper_executor, PaperExecutor):
            raise TypeError("paper_executor must be a PaperExecutor")
        self._risk_engine = risk_engine
        self._paper_executor = paper_executor
        self._orders: dict[UUID, Order] = {}
        self._results: dict[UUID, OrderSubmissionResult] = {}
        self._lock = RLock()

    @property
    def orders(self) -> dict[UUID, Order]:
        with self._lock:
            return dict(self._orders)

    def get_order(self, order_id: UUID) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def submit(
        self,
        order: Order,
        instrument: Instrument,
        portfolio: PaperPortfolio,
        risk_context: RiskContext,
        *,
        fill_price: float,
        filled_at: datetime | None = None,
        note: str = "",
    ) -> OrderSubmissionResult:
        """Evaluate risk before tracking or filling; retries are idempotent by order_id."""
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(instrument, Instrument):
            raise TypeError("instrument must be an Instrument")
        if not isinstance(portfolio, PaperPortfolio):
            raise TypeError("portfolio must be a PaperPortfolio")

        with self._lock:
            existing = self._results.get(order.order_id)
            if existing is not None:
                if not _same_request(existing.order, order):
                    return self._idempotency_conflict(order, existing)
                if existing.status is not OrderStatus.VALIDATED:
                    return replace(existing, duplicate=True)
                order = existing.order
                context = self._with_portfolio_state(
                    risk_context, portfolio, instrument
                )
                decision = self._risk_engine.evaluate(
                    replace(order, status=OrderStatus.NEW,
                            risk_decision_id=None),
                    instrument,
                    context,
                )
            else:
                context = self._with_portfolio_state(
                    risk_context, portfolio, instrument
                )
                decision = self._risk_engine.evaluate(
                    order, instrument, context)
                if decision.status is RiskDecisionStatus.APPROVED:
                    order = replace(
                        order,
                        status=OrderStatus.VALIDATED,
                        risk_decision_id=decision.decision_id,
                    )

            if decision.status is RiskDecisionStatus.REJECTED:
                rejected = replace(
                    order,
                    status=OrderStatus.REJECTED,
                    risk_decision_id=decision.decision_id,
                )
                result = OrderSubmissionResult(
                    order=rejected,
                    risk_decision=decision,
                    error=decision.reason,
                )
                self._orders[order.order_id] = rejected
                self._results[order.order_id] = result
                return result

            linked_order = replace(
                order, risk_decision_id=decision.decision_id)
            self._orders[order.order_id] = linked_order
            try:
                linked_order = self._transition(
                    linked_order, OrderStatus.SUBMITTED)
                self._orders[order.order_id] = linked_order
                fill = self._paper_executor.execute(
                    linked_order,
                    instrument,
                    portfolio,
                    risk_decision=decision,
                    fill_price=fill_price,
                    intent=context.intent,
                    filled_at=filled_at,
                    stop_loss=context.stop_loss,
                    take_profit=context.take_profit,
                    note=note,
                )
                filled_order = replace(
                    linked_order,
                    status=OrderStatus.FILLED,
                    filled_quantity=linked_order.quantity,
                    average_fill_price=fill.price,
                )
                self._orders[order.order_id] = filled_order
                result = OrderSubmissionResult(
                    order=filled_order,
                    risk_decision=decision,
                    fill=fill,
                )
            except Exception as exc:
                failed_order = replace(linked_order, status=OrderStatus.FAILED)
                self._orders[order.order_id] = failed_order
                result = OrderSubmissionResult(
                    order=failed_order,
                    risk_decision=decision,
                    error=f"PAPER_EXECUTION_FAILED: {exc}",
                )
            self._results[order.order_id] = result
            return result

    def stage(
        self,
        order: Order,
        instrument: Instrument,
        portfolio: PaperPortfolio,
        risk_context: RiskContext,
    ) -> OrderSubmissionResult:
        """Risk-check an order and leave it VALIDATED for later fill or cancellation."""
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(instrument, Instrument):
            raise TypeError("instrument must be an Instrument")
        if not isinstance(portfolio, PaperPortfolio):
            raise TypeError("portfolio must be a PaperPortfolio")
        with self._lock:
            existing = self._results.get(order.order_id)
            if existing is not None:
                if _same_request(existing.order, order):
                    return replace(existing, duplicate=True)
                return self._idempotency_conflict(order, existing)
            context = self._with_portfolio_state(
                risk_context, portfolio, instrument
            )
            decision = self._risk_engine.evaluate(order, instrument, context)
            status = (
                OrderStatus.VALIDATED
                if decision.status is RiskDecisionStatus.APPROVED
                else OrderStatus.REJECTED
            )
            staged = replace(
                order,
                status=status,
                risk_decision_id=decision.decision_id,
            )
            result = OrderSubmissionResult(
                order=staged,
                risk_decision=decision,
                error=decision.reason if status is OrderStatus.REJECTED else None,
            )
            self._orders[order.order_id] = staged
            self._results[order.order_id] = result
            return result

    def cancel(self, order_id: UUID) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"Unknown order_id: {order_id}")
            updated = self._transition(order, OrderStatus.CANCELLED)
            self._orders[order_id] = updated
            result = self._results.get(order_id)
            if result is not None:
                self._results[order_id] = replace(result, order=updated)
            return updated

    def transition(self, order_id: UUID, target: OrderStatus) -> Order:
        """Reject caller-driven execution edges; cancellation uses its dedicated path."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"Unknown order_id: {order_id}")
            if target is not OrderStatus.CANCELLED:
                raise InvalidOrderTransition(
                    "Only OrderManager may advance validation, submission, or fill states"
                )
            updated = self._transition(order, target)
            self._orders[order_id] = updated
            result = self._results.get(order_id)
            if result is not None:
                self._results[order_id] = replace(result, order=updated)
            return updated

    @staticmethod
    def _transition(order: Order, target: OrderStatus) -> Order:
        allowed = _TRANSITIONS.get(order.status, frozenset())
        if target not in allowed:
            raise InvalidOrderTransition(
                f"Invalid order transition: {order.status.value} -> {target.value}"
            )
        return replace(order, status=target)

    @staticmethod
    def _with_portfolio_state(
        context: RiskContext,
        portfolio: PaperPortfolio,
        instrument: Instrument,
    ) -> RiskContext:
        if not isinstance(context, RiskContext):
            return context
        position = portfolio.positions.get(instrument.instrument_id)
        return replace(
            context,
            available_cash=portfolio.cash,
            current_position_quantity=position.quantity if position else 0,
            current_position_side=position.side if position else None,
            open_positions=len(portfolio.positions),
        )

    @staticmethod
    def _idempotency_conflict(
        order: Order,
        existing: OrderSubmissionResult,
    ) -> OrderSubmissionResult:
        decision = RiskDecision(
            status=RiskDecisionStatus.REJECTED,
            timestamp=_aware_now(),
            reason=(
                "IDEMPOTENCY_KEY_REUSED: order_id was already used for a different request."
            ),
            order_id=order.order_id,
        )
        return OrderSubmissionResult(
            order=existing.order,
            risk_decision=decision,
            error=decision.reason,
            duplicate=True,
        )


def _same_request(first: Order, second: Order) -> bool:
    return replace(
        first,
        status=OrderStatus.NEW,
        filled_quantity=0,
        average_fill_price=None,
        risk_decision_id=None,
    ) == replace(
        second,
        status=OrderStatus.NEW,
        filled_quantity=0,
        average_fill_price=None,
        risk_decision_id=None,
    )


def _aware_now() -> datetime:
    return datetime.now().astimezone()
