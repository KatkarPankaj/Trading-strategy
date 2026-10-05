"""Broker abstraction: one interface, an in-memory PaperBroker, and an isolated adapter registry."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Mapping

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


class PaperBroker(BrokerAdapter):
    """In-memory simulated broker; fills are driven by update_price() and accounted in a PortfolioManager."""

    name = "paper"

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
            if order.instrument_id not in self._instruments:
                raise BrokerRejected(
                    f"unknown instrument {order.instrument_id}")
            if order.order_type is OrderType.STOP_LIMIT:
                raise BrokerRejected(
                    "STOP_LIMIT is not supported by the paper broker")
            if order.order_type is OrderType.MARKET and order.instrument_id not in self._prices:
                raise BrokerRejected("no price available for market order")
            self._seq += 1
            rec = _PaperOrder(order, f"PAPER-{self._seq}", order.quantity,
                              order.limit_price, order.stop_price)
            self._orders[rec.broker_order_id] = rec
            self._by_client[order.client_order_id] = rec.broker_order_id
            self._match(rec)
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

    def _match(self, rec: _PaperOrder) -> None:
        if rec.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
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
        instrument = self._instruments[rec.order.instrument_id]
        try:
            self._pf.apply_fill(
                instrument, rec.order.side, qty, fill_price, self._clock(),
                fee=self._fee_rate * fill_price * qty, slippage=abs(fill_price - price) * qty)
        except PortfolioError as exc:
            rec.status, rec.error = OrderStatus.REJECTED, str(exc)
            return
        rec.average_price = ((rec.average_price or 0.0) *
                             rec.filled + fill_price * qty) / (rec.filled + qty)
        rec.filled += qty
        rec.status = OrderStatus.FILLED

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
