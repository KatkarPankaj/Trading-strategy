"""Broker abstraction: one interface, an in-memory PaperBroker, and an isolated adapter registry."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isclose
from threading import RLock
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import Instrument, OrderSide, OrderStatus, OrderType, PositionSide
from .order_management import BrokerRejected, ManagedOrder
from .portfolio import PortfolioError, PortfolioManager


class BrokerError(Exception):
    pass


class BrokerDisconnected(BrokerError):
    pass


class BrokerConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    account_id: str
    base_currency: str
    equity: float
    cash: Mapping[str, float]
    buying_power: float


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument_id: str
    symbol: str
    side: PositionSide
    quantity: int
    average_entry_price: float
    currency: str


@dataclass(frozen=True, slots=True)
class BrokerOrderStatus:
    broker_order_id: str
    client_order_id: str
    status: OrderStatus
    quantity: int
    filled_quantity: int
    average_fill_price: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MarketStatus:
    market: str
    is_open: bool
    as_of: datetime
    session: str = ""


@dataclass(frozen=True, slots=True)
class BrokerCredentials:
    """Secrets come from the environment only; repr never shows them."""

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    account_id: str = ""

    @classmethod
    def from_env(cls, prefix: str, env: Mapping[str, str] | None = None) -> "BrokerCredentials":
        env = os.environ if env is None else env
        prefix = prefix.upper()
        key, secret = env.get(f"{prefix}_API_KEY"), env.get(
            f"{prefix}_API_SECRET")
        missing = [n for n, v in (
            (f"{prefix}_API_KEY", key), (f"{prefix}_API_SECRET", secret)) if not v]
        if missing:
            raise BrokerConfigError(
                f"missing environment variables: {', '.join(missing)}")
        # type: ignore[arg-type]
        return cls(api_key=key, api_secret=secret, account_id=env.get(f"{prefix}_ACCOUNT_ID", ""))


class BrokerAdapter(ABC):
    """Everything above this interface is broker-agnostic; broker specifics live in subclasses."""

    name: str
    supported_markets: frozenset[str]

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def account(self) -> BrokerAccount: ...

    @abstractmethod
    def positions(self) -> tuple[BrokerPosition, ...]: ...

    @abstractmethod
    def submit_order(self, order: ManagedOrder) -> str:
        """Return the broker_order_id; must be idempotent per client_order_id."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def modify_order(self, broker_order_id: str, *, quantity: int | None = None,
                     limit_price: float | None = None, stop_price: float | None = None) -> None: ...

    @abstractmethod
    def order_status(self, broker_order_id: str) -> BrokerOrderStatus: ...

    @abstractmethod
    def open_orders(self) -> tuple[BrokerOrderStatus, ...]: ...

    @abstractmethod
    def market_status(self, market: str) -> MarketStatus: ...

    # OrderGateway shape, so any adapter can sit behind OrderManager.
    def submit(self, order: ManagedOrder) -> str:
        return self.submit_order(order)

    def cancel(self, broker_order_id: str) -> None:
        self.cancel_order(broker_order_id)


class BrokerRegistry:
    """Maps a configured broker name to a factory; adapters register themselves here, not in strategy code."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., BrokerAdapter]] = {}

    def register(self, name: str, factory: Callable[..., BrokerAdapter]) -> None:
        key = name.strip().lower()
        if not key or key in self._factories:
            raise BrokerConfigError(
                f"invalid or duplicate broker name: {name!r}")
        self._factories[key] = factory

    def create(self, name: str, **kwargs: object) -> BrokerAdapter:
        try:
            factory = self._factories[name.strip().lower()]
        except KeyError:
            raise BrokerConfigError(
                f"unknown broker {name!r}; registered: {sorted(self._factories)}") from None
        return factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


@dataclass(slots=True)
class _PaperOrder:
    order: ManagedOrder
    broker_order_id: str
    quantity: int
    limit_price: float | None
    stop_price: float | None
    status: OrderStatus = OrderStatus.ACCEPTED
    filled: int = 0
    average_price: float | None = None
    error: str | None = None
    fill_events: int = 0


class PaperBroker(BrokerAdapter):
    """In-memory simulated broker; fills are driven by update_price() and accounted in a PortfolioManager."""

    name = "paper"

    @property
    def durable_execution(self) -> bool:
        return self._execution_repository is not None

    def __init__(
        self,
        portfolio: PortfolioManager,
        instruments: Mapping[str, Instrument],
        *,
        account_id: str = "PAPER",
        slippage_bps: float = 0.0,
        fee_rate: float = 0.0,
        clock: Callable[[], datetime] | None = None,
        market_status_fn: Callable[[str], bool] | None = None,
        execution_repository: object | None = None,
        fills_repository: object | None = None,
    ) -> None:
        if slippage_bps < 0 or fee_rate < 0:
            raise BrokerConfigError(
                "slippage_bps and fee_rate must be non-negative")
        self._pf = portfolio
        self._instruments = dict(instruments)
        self.supported_markets = frozenset(
            i.market for i in self._instruments.values())
        self._account_id = account_id
        self._slip = slippage_bps / 10_000
        self._fee_rate = fee_rate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._market_open = market_status_fn or (
            lambda market: True)  # paper default: always open
        self._execution_repository = execution_repository
        self._fills_repository = fills_repository
        self._connected = False
        self._orders: dict[str, _PaperOrder] = {}
        self._by_client: dict[str, str] = {}
        self._prices: dict[str, float] = {}
        self._price_times: dict[str, datetime] = {}
        self._seq = 0
        self._lock = RLock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        if self._execution_repository is not None:
            self._restore_execution_state()
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("paper broker is not connected")

    # ---- account ----
    def account(self) -> BrokerAccount:
        self._require_connected()
        with self._lock:
            return BrokerAccount(self._account_id, self._pf.base_currency, self._pf.equity,
                                 self._pf.cash, self._pf.cash_base)

    def positions(self) -> tuple[BrokerPosition, ...]:
        self._require_connected()
        with self._lock:
            return tuple(
                BrokerPosition(p.instrument_id, p.symbol, p.side, p.quantity,
                               p.average_entry_price, p.currency)
                for p in self._pf.positions().values())

    # ---- orders ----
    def submit_order(self, order: ManagedOrder) -> str:
        self._require_connected()
        with self._lock:
            existing = self._by_client.get(order.client_order_id)
            if existing is not None:
                return existing
            if self._execution_repository is not None:
                persisted = self._execution_repository.get_by_client_order_id(
                    order.client_order_id)
                if persisted is not None:
                    self._restore_order(persisted)
                    return persisted["broker_order_id"]
            if order.instrument_id not in self._instruments:
                self._record_rejection(order, f"unknown instrument {order.instrument_id}")
                raise BrokerRejected(f"unknown instrument {order.instrument_id}")
            if order.order_type is OrderType.STOP_LIMIT:
                self._record_rejection(order, "STOP_LIMIT is not supported by the paper broker")
                raise BrokerRejected(
                    "STOP_LIMIT is not supported by the paper broker")
            if order.order_type is OrderType.MARKET and order.instrument_id not in self._prices:
                self._record_rejection(order, "no price available for market order")
                raise BrokerRejected("no price available for market order")
            broker_id = "PAPER-" + uuid5(
                NAMESPACE_URL, f"paper-execution:{order.client_order_id}").hex
            rec = _PaperOrder(
                order, broker_id, order.quantity, order.limit_price, order.stop_price)
            self._orders[rec.broker_order_id] = rec
            self._by_client[order.client_order_id] = rec.broker_order_id
            portfolio_checkpoint = self._pf.execution_checkpoint()
            try:
                if self._execution_repository is not None:
                    attempts = self._execution_repository.attempts(
                        order.client_order_id)
                    attempt_number = len(attempts) + 1
                    attempt_id = str(uuid5(
                        NAMESPACE_URL,
                        f"paper-execution-attempt:{order.client_order_id}:{attempt_number}"))
                    with self._execution_repository.transaction():
                        self._save_rec(rec)
                        self._execution_repository.save_attempt(
                            attempt_id=attempt_id,
                            client_order_id=order.client_order_id,
                            attempt_number=attempt_number,
                            state="SUBMITTING")
                        self._match(rec, persist=False)
                        self._execution_repository.save_attempt(
                            attempt_id=attempt_id,
                            client_order_id=order.client_order_id,
                            attempt_number=attempt_number,
                            state=rec.status.value,
                            error=rec.error)
                        self._save_rec(rec)
                else:
                    self._match(rec)
            except BaseException:
                self._pf.restore_execution_checkpoint(portfolio_checkpoint)
                self._orders.pop(rec.broker_order_id, None)
                self._by_client.pop(order.client_order_id, None)
                raise
            if rec.status is OrderStatus.REJECTED:
                raise BrokerRejected(rec.error or "rejected")
            return rec.broker_order_id

    def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        with self._lock:
            rec = self._get(broker_order_id)
            if rec.status is OrderStatus.CANCELLED:
                return
            if rec.status in (OrderStatus.FILLED, OrderStatus.REJECTED):
                raise BrokerError(f"cannot cancel {rec.status.value} order")
            rec.status = OrderStatus.CANCELLED
            self._save_rec(rec)

    def modify_order(self, broker_order_id: str, *, quantity: int | None = None,
                     limit_price: float | None = None, stop_price: float | None = None) -> None:
        self._require_connected()
        with self._lock:
            rec = self._get(broker_order_id)
            if rec.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
                raise BrokerError(f"cannot modify {rec.status.value} order")
            if quantity is not None:
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= rec.filled:
                    raise BrokerError(
                        "quantity must be an integer above the filled quantity")
                rec.quantity = quantity
            if limit_price is not None:
                if rec.limit_price is None or not limit_price > 0:
                    raise BrokerError("invalid limit_price change")
                rec.limit_price = limit_price
            if stop_price is not None:
                if rec.stop_price is None or not stop_price > 0:
                    raise BrokerError("invalid stop_price change")
                rec.stop_price = stop_price
            self._match(rec)

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        self._require_connected()
        with self._lock:
            return self._view(self._get(broker_order_id))

    def open_orders(self) -> tuple[BrokerOrderStatus, ...]:
        self._require_connected()
        with self._lock:
            return tuple(self._view(r) for r in self._orders.values()
                         if r.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED))

    def order_status_by_client_id(
        self, client_order_id: str,
    ) -> BrokerOrderStatus | None:
        """Look up any durable paper order by its stable client identity."""
        self._require_connected()
        with self._lock:
            broker_id = self._by_client.get(client_order_id)
            if broker_id is None:
                if self._execution_repository is None:
                    return None
                row = self._execution_repository.get_by_client_order_id(client_order_id)
                if row is None:
                    return None
                self._restore_order(row)
                broker_id = row["broker_order_id"]
            return self._view(self._orders[broker_id])

    def market_status(self, market: str) -> MarketStatus:
        self._require_connected()
        return MarketStatus(market, bool(self._market_open(market)), self._clock(), "paper")

    # ---- simulation ----
    def last_quote(self, instrument_id: str) -> tuple[float, datetime] | None:
        with self._lock:
            price = self._prices.get(instrument_id)
            return None if price is None else (price, self._price_times[instrument_id])

    def update_price(self, instrument_id: str, price: float, timestamp: datetime | None = None) -> None:
        """Feed a market price; marks positions and fills any resting orders that trigger."""
        self._require_connected()
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price < float("inf"):
            raise BrokerError("price must be finite and greater than zero")
        with self._lock:
            self._prices[instrument_id] = float(price)
            self._price_times[instrument_id] = timestamp or self._clock()
            if instrument_id in self._pf.positions():
                self._pf.mark(instrument_id, price, timestamp or self._clock())
            for rec in list(self._orders.values()):
                if rec.order.instrument_id == instrument_id:
                    self._match(rec)

    def _match(self, rec: _PaperOrder, *, persist: bool = True) -> None:
        if rec.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
            return
        instrument = self._instruments[rec.order.instrument_id]
        if not self._market_open(instrument.market):
            return
        price = self._prices.get(rec.order.instrument_id)
        if price is None:
            return
        buy = rec.order.side is OrderSide.BUY
        kind = rec.order.order_type
        if kind is OrderType.MARKET:
            trigger = True
        elif kind is OrderType.LIMIT:
            # type: ignore[operator]
            trigger = price <= rec.limit_price if buy else price >= rec.limit_price
        else:
            # type: ignore[operator]
            trigger = price >= rec.stop_price if buy else price <= rec.stop_price
        if not trigger:
            return
        slip_price = price * \
            (1 + self._slip) if buy else price * (1 - self._slip)
        fill_price = price if kind is OrderType.LIMIT else slip_price
        qty = rec.quantity - rec.filled
        try:
            self._pf.apply_fill(
                instrument, rec.order.side, qty, fill_price, self._clock(),
                fee=self._fee_rate * fill_price * qty,
                slippage=abs(fill_price - price) * qty,
                client_order_id=rec.order.client_order_id,
                fill_sequence=rec.fill_events + 1)
        except PortfolioError as exc:
            rec.status, rec.error = OrderStatus.REJECTED, str(exc)
            if persist:
                self._save_rec(rec)
            return
        rec.average_price = ((rec.average_price or 0.0) *
                             rec.filled + fill_price * qty) / (rec.filled + qty)
        rec.filled += qty
        rec.fill_events += 1
        rec.status = OrderStatus.FILLED
        if persist:
            self._save_rec(rec)

    def _record_rejection(self, order: ManagedOrder, error: str) -> None:
        if self._execution_repository is None:
            return
        broker_id = "PAPER-" + uuid5(
            NAMESPACE_URL, f"paper-execution:{order.client_order_id}").hex
        rec = _PaperOrder(
            order, broker_id, order.quantity, order.limit_price,
            order.stop_price, status=OrderStatus.REJECTED, error=error)
        attempts = self._execution_repository.attempts(order.client_order_id)
        attempt_number = len(attempts) + 1
        attempt_id = str(uuid5(
            NAMESPACE_URL,
            f"paper-execution-attempt:{order.client_order_id}:{attempt_number}"))
        with self._execution_repository.transaction():
            self._save_rec(rec)
            self._execution_repository.save_attempt(
                attempt_id=attempt_id, client_order_id=order.client_order_id,
                attempt_number=attempt_number, state=OrderStatus.REJECTED.value,
                error=error)
        self._orders[broker_id] = rec
        self._by_client[order.client_order_id] = broker_id

    def _save_rec(self, rec: _PaperOrder) -> None:
        if self._execution_repository is None:
            return
        self._execution_repository.save_order(
            client_order_id=rec.order.client_order_id,
            broker_order_id=rec.broker_order_id,
            instrument_id=rec.order.instrument_id,
            status=rec.status.value,
            quantity=rec.quantity,
            limit_price=rec.limit_price,
            stop_price=rec.stop_price,
            filled_quantity=rec.filled,
            average_fill_price=rec.average_price,
            error=rec.error,
            fill_events=rec.fill_events,
            order_payload={
                "client_order_id": rec.order.client_order_id,
                "broker_order_id": rec.order.broker_order_id,
                "instrument_id": rec.order.instrument_id,
                "symbol": rec.order.symbol,
                "side": rec.order.side.value,
                "quantity": rec.order.quantity,
                "order_type": rec.order.order_type.value,
                "limit_price": rec.order.limit_price,
                "stop_price": rec.order.stop_price,
                "timestamp": rec.order.timestamp.isoformat(),
                "strategy": rec.order.strategy,
                "signal_id": str(rec.order.signal_id) if rec.order.signal_id else None,
                "risk_decision_id": (
                    str(rec.order.risk_decision_id)
                    if rec.order.risk_decision_id else None),
            },
        )

    def _restore_execution_state(self) -> None:
        self._orders.clear()
        self._by_client.clear()
        for row in self._execution_repository.all():
            self._restore_order(row)

    def _restore_order(self, row: Mapping[str, object]) -> None:
        payload = row["order_payload"]
        fills = (
            self._fills_repository.for_order(str(row["client_order_id"]))
            if self._fills_repository is not None else [])
        filled = sum(int(fill["quantity"]) for fill in fills)
        if filled > int(row["quantity"]):
            raise BrokerError(
                f"persisted paper fills exceed order quantity for {row['client_order_id']}")
        fill_events = int(row["fill_events"])
        sequences = [int(fill["fill_sequence"] or 0) for fill in fills]
        if (
            filled != int(row["filled_quantity"])
            or fill_events != len(fills)
            or sorted(sequences) != list(range(1, fill_events + 1))
        ):
            raise BrokerError(
                f"persisted paper execution and fill ledger disagree for {row['client_order_id']}")
        weighted = sum(float(fill["price"]) * int(fill["quantity"]) for fill in fills)
        average = weighted / filled if filled else row["average_fill_price"]
        status = OrderStatus(str(row["status"]))
        if filled:
            expected = (
                OrderStatus.FILLED if filled == int(row["quantity"])
                else OrderStatus.PARTIALLY_FILLED)
            allowed = {expected}
            if expected is OrderStatus.PARTIALLY_FILLED:
                allowed.add(OrderStatus.CANCELLED)
            if status not in allowed:
                raise BrokerError(
                    f"persisted paper execution status conflicts with fills for {row['client_order_id']}")
            if average is None or row["average_fill_price"] is None or not isclose(
                float(average), float(row["average_fill_price"]),
                rel_tol=1e-9, abs_tol=1e-9,
            ):
                raise BrokerError(
                    f"persisted paper execution average conflicts with fills for {row['client_order_id']}")
        elif status in {
            OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        } or row["average_fill_price"] is not None:
            raise BrokerError(
                f"persisted paper execution has fill state without fills for {row['client_order_id']}")
        managed = ManagedOrder(
            client_order_id=str(payload["client_order_id"]),
            broker_order_id=payload.get("broker_order_id"),
            instrument_id=str(payload["instrument_id"]),
            symbol=str(payload["symbol"]),
            side=OrderSide(str(payload["side"])),
            quantity=int(payload["quantity"]),
            order_type=OrderType(str(payload["order_type"])),
            limit_price=payload.get("limit_price"),
            stop_price=payload.get("stop_price"),
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
            strategy=str(payload["strategy"]),
            signal_id=(
                UUID(str(payload["signal_id"])) if payload.get("signal_id") else None),
            risk_decision_id=(
                UUID(str(payload["risk_decision_id"]))
                if payload.get("risk_decision_id") else None),
            status=OrderStatus.SUBMITTED,
        )
        rec = _PaperOrder(
            managed,
            str(row["broker_order_id"]),
            int(row["quantity"]),
            row["limit_price"],
            row["stop_price"],
            status=status,
            filled=filled,
            average_price=average,
            error=row["error"],
            fill_events=fill_events,
        )
        self._orders[rec.broker_order_id] = rec
        self._by_client[managed.client_order_id] = rec.broker_order_id

    def _get(self, broker_order_id: str) -> _PaperOrder:
        try:
            return self._orders[broker_order_id]
        except KeyError:
            raise BrokerError(
                f"unknown broker order {broker_order_id}") from None

    @staticmethod
    def _view(rec: _PaperOrder) -> BrokerOrderStatus:
        return BrokerOrderStatus(rec.broker_order_id, rec.order.client_order_id, rec.status,
                                 rec.quantity, rec.filled, rec.average_price, rec.error)
