"""Broker-independent order lifecycle: risk-gated, idempotent, duplicate-safe state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from .models import (
    OrderSide,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskDecisionStatus,
)

TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FAILED,
})

_S = OrderStatus
TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    _S.NEW: frozenset({_S.VALIDATED, _S.REJECTED, _S.FAILED}),
    _S.VALIDATED: frozenset({_S.SUBMITTED, _S.REJECTED, _S.FAILED}),
    _S.SUBMITTED: frozenset({_S.ACCEPTED, _S.PARTIALLY_FILLED, _S.FILLED,
                             _S.CANCEL_PENDING, _S.REJECTED, _S.FAILED}),
    _S.ACCEPTED: frozenset({_S.PARTIALLY_FILLED, _S.FILLED, _S.CANCEL_PENDING, _S.FAILED}),
    _S.PARTIALLY_FILLED: frozenset({_S.PARTIALLY_FILLED, _S.FILLED, _S.CANCEL_PENDING, _S.FAILED}),
    _S.CANCEL_PENDING: frozenset({_S.CANCELLED, _S.PARTIALLY_FILLED, _S.FILLED, _S.FAILED}),
    _S.FILLED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.FAILED: frozenset(),
}


class OrderManagerError(Exception):
    pass


class InvalidOrderTransition(OrderManagerError):
    pass


class IdempotencyConflict(OrderManagerError):
    """client_order_id was already used for a different request."""


class UnknownOrder(OrderManagerError, KeyError):
    pass


class BrokerRejected(Exception):
    """Raised by a gateway when the broker definitively refuses an order."""


class OrderGateway(Protocol):
    """Minimal outbound port; BrokerAdapter implementations satisfy it."""

    def submit(self, order: "ManagedOrder") -> str:
        """Send the order and return the broker_order_id."""

    def cancel(self, broker_order_id: str) -> None: ...


def new_client_order_id() -> str:
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    timestamp: datetime
    strategy: str
    signal_id: UUID | None = None
    limit_price: float | None = None
    stop_price: float | None = None

    def __post_init__(self) -> None:
        for name in ("client_order_id", "instrument_id", "symbol", "strategy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.side, OrderSide) or not isinstance(self.order_type, OrderType):
            raise TypeError("side and order_type must be enum members")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if (not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None
                or self.timestamp.utcoffset() is None):
            raise ValueError("timestamp must be timezone-aware")
        if self.signal_id is not None and not isinstance(self.signal_id, UUID):
            raise TypeError("signal_id must be a UUID")
        for name in ("limit_price", "stop_price"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not value > 0 or value == float("inf")
            ):
                raise ValueError(
                    f"{name} must be finite and greater than zero")
        needs_limit = self.order_type in (
            OrderType.LIMIT, OrderType.STOP_LIMIT)
        needs_stop = self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
        if needs_limit != (self.limit_price is not None):
            raise ValueError(
                "limit_price is required for, and only for, limit order types")
        if needs_stop != (self.stop_price is not None):
            raise ValueError(
                "stop_price is required for, and only for, stop order types")

    def fingerprint(self) -> tuple:
        """Everything except the idempotency key and timestamp."""
        return (self.instrument_id, self.symbol, self.side, self.quantity, self.order_type,
                self.limit_price, self.stop_price, self.strategy, self.signal_id)


@dataclass(frozen=True, slots=True)
class OrderEvent:
    timestamp: datetime
    from_status: OrderStatus | None
    to_status: OrderStatus
    detail: str = ""
    fill_quantity: int | None = None
    fill_price: float | None = None


@dataclass(frozen=True, slots=True)
class ManagedOrder:
    client_order_id: str
    broker_order_id: str | None
    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: float | None
    stop_price: float | None
    timestamp: datetime
    strategy: str
    signal_id: UUID | None
    risk_decision_id: UUID | None
    status: OrderStatus
    filled_quantity: int = 0
    average_fill_price: float | None = None
    error: str | None = None

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    order: ManagedOrder
    duplicate: bool = False  # True when an earlier result is returned and nothing was sent

    @property
    def status(self) -> OrderStatus:
        return self.order.status


class OrderManager:
    """The only component allowed to move orders through the lifecycle."""

    def __init__(
        self,
        gateway: OrderGateway,
        *,
        clock: Callable[[], datetime] | None = None,
        max_risk_decision_age: timedelta = timedelta(seconds=60),
        on_change: Callable[[ManagedOrder, OrderEvent], None] | None = None,
    ) -> None:
        if max_risk_decision_age <= timedelta(0):
            raise ValueError("max_risk_decision_age must be positive")
        # Called after every transition, before the next step; used for write-ahead persistence.
        self.on_change = on_change
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_age = max_risk_decision_age
        self._orders: dict[str, ManagedOrder] = {}
        self._requests: dict[str, OrderRequest] = {}
        self._events: dict[str, list[OrderEvent]] = {}
        self._safe_retry: set[str] = set()
        self._lock = RLock()

    # ---- queries ----
    def get(self, client_order_id: str) -> ManagedOrder:
        with self._lock:
            try:
                return self._orders[client_order_id]
            except KeyError:
                raise UnknownOrder(client_order_id) from None

    def orders(self) -> tuple[ManagedOrder, ...]:
        with self._lock:
            return tuple(self._orders.values())

    def open_orders(self) -> tuple[ManagedOrder, ...]:
        with self._lock:
            return tuple(o for o in self._orders.values() if not o.is_terminal)

    def events(self, client_order_id: str) -> tuple[OrderEvent, ...]:
        with self._lock:
            if client_order_id not in self._events:
                raise UnknownOrder(client_order_id)
            return tuple(self._events[client_order_id])

    # ---- submission ----
    def submit(self, request: OrderRequest, risk_decision: RiskDecision) -> SubmissionResult:
        """Idempotent by client_order_id; an order never reaches the gateway without APPROVED risk."""
        with self._lock:
            known = self._requests.get(request.client_order_id)
            retry = (
                known is not None
                and request.client_order_id in self._safe_retry
                and self._orders[request.client_order_id].status is OrderStatus.NEW
            )
            if known is not None:
                if known.fingerprint() != request.fingerprint():
                    raise IdempotencyConflict(
                        f"client_order_id {request.client_order_id} was used for a different request")
                if not retry:
                    return SubmissionResult(self._orders[request.client_order_id], duplicate=True)
                self._safe_retry.remove(request.client_order_id)
                order = self._orders[request.client_order_id]
            else:
                order = ManagedOrder(
                    client_order_id=request.client_order_id, broker_order_id=None,
                    instrument_id=request.instrument_id, symbol=request.symbol,
                    side=request.side, quantity=request.quantity, order_type=request.order_type,
                    limit_price=request.limit_price, stop_price=request.stop_price,
                    timestamp=request.timestamp, strategy=request.strategy,
                    signal_id=request.signal_id, risk_decision_id=None, status=OrderStatus.NEW)
                self._requests[request.client_order_id] = request
                self._orders[request.client_order_id] = order
                self._events[request.client_order_id] = []
                self._log(order, None, OrderStatus.NEW, "created")

            problem = self._pre_trade_problem(request, risk_decision)
            if problem is not None:
                return SubmissionResult(self._move(order, OrderStatus.REJECTED, error=problem))

            order = self._move(replace(order, risk_decision_id=risk_decision.decision_id),
                               OrderStatus.VALIDATED, detail="risk approved")
            order = self._move(order, OrderStatus.SUBMITTED)
            try:
                broker_id = self._gateway.submit(order)
            except BrokerRejected as exc:
                order = self._move(order, OrderStatus.REJECTED,
                                   error=f"BROKER_REJECTED: {exc}")
            except Exception as exc:  # broker may or may not have the order; never auto-retry
                order = self._move(
                    order, OrderStatus.FAILED,
                    error=f"SUBMIT_FAILED_STATE_UNKNOWN: {exc}; reconcile with broker before retrying")
            else:
                if not isinstance(broker_id, str) or not broker_id.strip():
                    order = self._move(order, OrderStatus.FAILED,
                                       error="GATEWAY_RETURNED_NO_BROKER_ORDER_ID")
                else:
                    order = self._move(replace(order, broker_order_id=broker_id),
                                       OrderStatus.ACCEPTED, detail="broker accepted")
            return SubmissionResult(order)

    def _pre_trade_problem(self, request: OrderRequest, decision: RiskDecision) -> str | None:
        if not isinstance(decision, RiskDecision):
            return "RISK_DECISION_MISSING"
        if decision.status is not RiskDecisionStatus.APPROVED:
            return f"RISK_REJECTED: {decision.reason}"
        if request.signal_id is not None and decision.signal_id not in (None, request.signal_id):
            return "RISK_DECISION_SIGNAL_MISMATCH"
        age = self._clock() - decision.timestamp
        if age < timedelta(0) or age > self._max_age:
            return "RISK_DECISION_STALE_OR_FUTURE"
        for other in self._orders.values():
            if other.client_order_id == request.client_order_id or other.is_terminal:
                continue
            if self._duplicates(other, request):
                return f"DUPLICATE_ORDER: equivalent order {other.client_order_id} is still active"
        return None

    @staticmethod
    def _duplicates(other: ManagedOrder, request: OrderRequest) -> bool:
        if request.signal_id is not None and other.signal_id == request.signal_id:
            return other.instrument_id == request.instrument_id and other.side is request.side
        return (other.instrument_id, other.side, other.quantity, other.order_type,
                other.limit_price, other.stop_price, other.strategy) == (
            request.instrument_id, request.side, request.quantity, request.order_type,
            request.limit_price, request.stop_price, request.strategy)

    # ---- broker-driven events ----
    def apply_fill(self, client_order_id: str, quantity: int, price: float,
                   timestamp: datetime | None = None) -> ManagedOrder:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("fill quantity must be a positive integer")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price < float("inf"):
            raise ValueError("fill price must be finite and greater than zero")
        with self._lock:
            order = self.get(client_order_id)
            if quantity > order.remaining_quantity:
                raise OrderManagerError(
                    f"fill quantity {quantity} exceeds remaining {order.remaining_quantity}")
            total = order.filled_quantity + quantity
            average = ((order.average_fill_price or 0.0) * order.filled_quantity
                       + price * quantity) / total
            target = OrderStatus.FILLED if total == order.quantity else OrderStatus.PARTIALLY_FILLED
            updated = replace(order, filled_quantity=total,
                              average_fill_price=average)
            return self._move(updated, target, detail=f"fill {quantity}@{price}", at=timestamp,
                              fill=(quantity, float(price)))

    def sync(self, client_order_id: str) -> ManagedOrder:
        """Pull fills and cancellations from the broker if the gateway can report order status."""
        with self._lock:
            order = self.get(client_order_id)
            query = getattr(self._gateway, "order_status", None)
            if order.is_terminal or order.broker_order_id is None or query is None:
                return order
            status = query(order.broker_order_id)
            delta = status.filled_quantity - order.filled_quantity
            if delta > 0 and status.average_fill_price is not None:
                previous = (order.average_fill_price or 0.0) * \
                    order.filled_quantity
                price = (status.average_fill_price *
                         status.filled_quantity - previous) / delta
                return self.apply_fill(client_order_id, delta, price)
            if status.status is OrderStatus.CANCELLED and order.status is OrderStatus.CANCEL_PENDING:
                return self.confirm_cancel(client_order_id)
            return order

    def confirm_cancel(self, client_order_id: str) -> ManagedOrder:
        """Record the broker's cancellation confirmation."""
        with self._lock:
            return self._move(self.get(client_order_id), OrderStatus.CANCELLED, detail="broker confirmed")

    def mark_failed(self, client_order_id: str, error: str) -> ManagedOrder:
        with self._lock:
            return self._move(self.get(client_order_id), OrderStatus.FAILED, error=error)

    def cancel(self, client_order_id: str) -> ManagedOrder:
        """Request cancellation; idempotent while CANCEL_PENDING or CANCELLED."""
        with self._lock:
            order = self.get(client_order_id)
            if order.status in (OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED):
                return order
            order = self._move(
                order, OrderStatus.CANCEL_PENDING, detail="cancel requested")
            if order.broker_order_id is None:
                raise OrderManagerError(
                    "order has no broker_order_id to cancel")
            # failure propagates; order stays CANCEL_PENDING
            self._gateway.cancel(order.broker_order_id)
            return order

    def broker_status(self, client_order_id: str) -> Any:
        """The gateway's own view of the order (kept verbatim for the audit trail), or None if unsupported."""
        with self._lock:
            order = self.get(client_order_id)
            query = getattr(self._gateway, "order_status", None)
            if query is None or order.broker_order_id is None:
                return None
            return query(order.broker_order_id)

    # ---- recovery ----
    def restore(self, order: ManagedOrder, events: tuple[OrderEvent, ...] = ()) -> None:
        """Load a persisted order after a restart without contacting the gateway or firing hooks."""
        with self._lock:
            if order.client_order_id in self._orders:
                return
            self._orders[order.client_order_id] = order
            self._events[order.client_order_id] = list(events)
            self._requests[order.client_order_id] = OrderRequest(
                client_order_id=order.client_order_id, instrument_id=order.instrument_id,
                symbol=order.symbol, side=order.side, quantity=order.quantity,
                order_type=order.order_type, timestamp=order.timestamp, strategy=order.strategy,
                signal_id=order.signal_id, limit_price=order.limit_price, stop_price=order.stop_price)

    def authorize_safe_retry(self, client_order_id: str) -> ManagedOrder:
        """Rewind only an unacknowledged order proven absent by a durable paper executor."""
        with self._lock:
            order = self.get(client_order_id)
            if order.status not in {
                OrderStatus.NEW, OrderStatus.VALIDATED, OrderStatus.SUBMITTED,
                OrderStatus.FAILED,
            } or order.broker_order_id is not None:
                raise InvalidOrderTransition(
                    "only an unacknowledged order can be safely retried")
            updated = replace(
                order, status=OrderStatus.NEW, risk_decision_id=None, error=None)
            self._orders[client_order_id] = updated
            self._safe_retry.add(client_order_id)
            self._log(
                updated, order.status, OrderStatus.NEW,
                "recovery proved no paper execution exists; retry authorized")
            return updated

    def reconcile_execution(
        self,
        client_order_id: str,
        *,
        broker_order_id: str,
        status: OrderStatus,
        filled_quantity: int,
        average_fill_price: float | None,
        error: str | None,
    ) -> ManagedOrder:
        """Adopt executor state without applying fills already replayed from durable fill records."""
        with self._lock:
            order = self.get(client_order_id)
            if filled_quantity < order.filled_quantity or filled_quantity > order.quantity:
                raise OrderManagerError(
                    "durable executor fill quantity conflicts with the stored order")
            if order.broker_order_id not in (None, broker_order_id):
                raise OrderManagerError(
                    "durable executor broker identity conflicts with the stored order")
            if order.status is not status or (
                order.filled_quantity != filled_quantity
                or order.average_fill_price != average_fill_price
                or order.broker_order_id != broker_order_id
                or order.error != error
            ):
                updated = replace(
                    order,
                    broker_order_id=broker_order_id,
                    status=status,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    error=error,
                )
                self._orders[client_order_id] = updated
                self._log(
                    updated, order.status, status,
                    "durable paper execution state reconciled")
                return updated
            return order

    def attach_broker_order(self, client_order_id: str, broker_order_id: str) -> ManagedOrder:
        """Record a broker id discovered during reconciliation for an order whose acknowledgement was lost."""
        with self._lock:
            order = self.get(client_order_id)
            if order.status is not OrderStatus.SUBMITTED or order.broker_order_id is not None:
                raise InvalidOrderTransition(
                    "only an unacknowledged SUBMITTED order can adopt a broker id")
            return self._move(replace(order, broker_order_id=broker_order_id), OrderStatus.ACCEPTED,
                              detail="broker id recovered during reconciliation")

    # ---- internals ----
    def _move(self, order: ManagedOrder, target: OrderStatus, *, detail: str = "",
              error: str | None = None, at: datetime | None = None,
              fill: tuple[int, float] | None = None) -> ManagedOrder:
        current = self._orders[order.client_order_id]
        if target not in TRANSITIONS[current.status]:
            raise InvalidOrderTransition(
                f"{current.status.value} -> {target.value}")
        updated = replace(order, status=target,
                          error=error if error is not None else order.error)
        self._orders[order.client_order_id] = updated
        self._log(updated, current.status, target,
                  detail or (error or ""), at, fill)
        return updated

    def _log(self, order: ManagedOrder, previous: OrderStatus | None, target: OrderStatus,
             detail: str, at: datetime | None = None, fill: tuple[int, float] | None = None) -> None:
        self._events[order.client_order_id].append(
            OrderEvent(at or self._clock(), previous, target, detail,
                       fill[0] if fill else None, fill[1] if fill else None))
        if self.on_change is not None:
            self.on_change(order, self._events[order.client_order_id][-1])
