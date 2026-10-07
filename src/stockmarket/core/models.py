"""Typed, framework-independent trading domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import Enum
from math import isfinite
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"
    FX = "FX"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class TradingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class RiskDecisionStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")


def _require_enum(value: Enum, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_positive_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")


def _require_non_negative_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _require_positive_integer(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")


def _require_optional_price(value: float | None, field_name: str) -> None:
    if value is not None:
        _require_positive_number(value, field_name)


def _require_string_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field_name} must be a tuple of non-empty strings")


@dataclass(frozen=True, slots=True)
class Instrument:
    """A market instrument identified independently of any data or broker API."""

    instrument_id: str
    symbol: str
    exchange: str
    market: str
    asset_class: AssetClass
    currency: str
    timezone: str
    tick_size: float
    lot_size: int = 1
    trading_hours: tuple[time, time] | None = None
    price_precision: int = 2
    minimum_order_quantity: int = 1
    shortable: bool | None = None
    trading_status: TradingStatus = TradingStatus.ACTIVE
    name: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    isin: str | None = None
    figi: str | None = None
    cusip: str | None = None
    mic: str | None = None
    exchange_symbol: str | None = None
    provider_symbol: str | None = None
    active: bool = True
    tradable: bool = True
    market_cap: float | None = None

    def __post_init__(self) -> None:
        for name in ("instrument_id", "symbol", "exchange", "market", "currency"):
            _require_text(getattr(self, name), name)
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("currency must be an uppercase 3-letter currency code")
        _require_enum(self.asset_class, AssetClass, "asset_class")
        _require_enum(self.trading_status, TradingStatus, "trading_status")
        _require_positive_number(self.tick_size, "tick_size")
        _require_positive_integer(self.lot_size, "lot_size")
        _require_positive_integer(
            self.minimum_order_quantity, "minimum_order_quantity"
        )
        if isinstance(self.price_precision, bool) or not isinstance(
            self.price_precision, int
        ):
            raise TypeError("price_precision must be an integer")
        if self.price_precision < 0:
            raise ValueError("price_precision must be non-negative")
        tick_precision = max(
            0, -Decimal(str(self.tick_size)).normalize().as_tuple().exponent
        )
        if tick_precision > self.price_precision:
            raise ValueError(
                "price_precision must be sufficient to represent tick_size"
            )
        if self.shortable is not None and not isinstance(self.shortable, bool):
            raise TypeError("shortable must be a bool or None")
        for name in (
            "name", "country", "sector", "industry", "isin", "figi", "cusip",
            "mic", "exchange_symbol", "provider_symbol",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if not isinstance(self.active, bool) or not isinstance(self.tradable, bool):
            raise TypeError("active and tradable must be bools")
        if self.market_cap is not None:
            _require_positive_number(self.market_cap, "market_cap")
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("timezone must be a valid IANA timezone name")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        if self.trading_hours is not None:
            if (
                not isinstance(self.trading_hours, tuple)
                or len(self.trading_hours) != 2
                or any(
                    not isinstance(value, time) or value.tzinfo is not None
                    for value in self.trading_hours
                )
            ):
                raise ValueError(
                    "trading_hours must be a pair of timezone-naive local times"
                )

    def is_valid_price(self, price: object) -> bool:
        """Return whether a positive price lies on this instrument's tick grid."""
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not isfinite(price)
            or price <= 0
        ):
            return False
        return (
            Decimal(str(price)) % Decimal(str(self.tick_size))
        ) == Decimal(0)

    def is_valid_order_quantity(self, quantity: object) -> bool:
        """Return whether a positive quantity satisfies minimum and lot-size rules."""
        return (
            isinstance(quantity, int)
            and not isinstance(quantity, bool)
            and quantity >= self.minimum_order_quantity
            and quantity % self.lot_size == 0
        )


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy observation; it is not an order or an execution instruction."""

    instrument_id: str
    symbol: str
    timestamp: datetime
    strategy: str
    side: SignalSide
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reward_risk: float | None = None
    confidence: float = 0.0
    expected_edge: float = 0.0
    regime: str = "UNKNOWN"
    reasons: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    signal_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.symbol, "symbol")
        _require_text(self.strategy, "strategy")
        _require_text(self.regime, "regime")
        _require_enum(self.side, SignalSide, "side")
        _require_aware_datetime(self.timestamp, "timestamp")
        _require_uuid(self.signal_id, "signal_id")
        _require_optional_price(self.entry_price, "entry_price")
        _require_optional_price(self.stop_loss, "stop_loss")
        _require_optional_price(self.take_profit, "take_profit")
        if self.reward_risk is not None:
            _require_non_negative_number(self.reward_risk, "reward_risk")
        _require_non_negative_number(self.confidence, "confidence")
        if self.confidence > 100:
            raise ValueError("confidence must be between 0 and 100")
        if isinstance(self.expected_edge, bool) or not isinstance(
            self.expected_edge, (int, float)
        ) or not isfinite(self.expected_edge):
            raise ValueError("expected_edge must be a finite number")
        _require_string_tuple(self.reasons, "reasons")
        _require_string_tuple(
            self.invalidation_conditions, "invalidation_conditions"
        )
        if self.side is SignalSide.HOLD and any(
            value is not None
            for value in (self.entry_price, self.stop_loss, self.take_profit)
        ):
            raise ValueError(
                "HOLD signals cannot include entry or exit prices")
        if self.entry_price is not None:
            if self.side is SignalSide.BUY and self.stop_loss is not None:
                if self.stop_loss >= self.entry_price:
                    raise ValueError(
                        "BUY signal stop_loss must be below entry_price")
            if self.side is SignalSide.BUY and self.take_profit is not None:
                if self.take_profit <= self.entry_price:
                    raise ValueError(
                        "BUY signal take_profit must be above entry_price")
            if self.side is SignalSide.SELL and self.stop_loss is not None:
                if self.stop_loss <= self.entry_price:
                    raise ValueError(
                        "SELL signal stop_loss must be above entry_price")
            if self.side is SignalSide.SELL and self.take_profit is not None:
                if self.take_profit >= self.entry_price:
                    raise ValueError(
                        "SELL signal take_profit must be below entry_price")


@dataclass(frozen=True, slots=True)
class Order:
    """A broker-independent paper/order-intent record."""

    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    created_at: datetime
    limit_price: float | None = None
    stop_price: float | None = None
    strategy: str = ""
    signal_id: UUID | None = None
    risk_decision_id: UUID | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    average_fill_price: float | None = None
    order_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.symbol, "symbol")
        if self.strategy:
            _require_text(self.strategy, "strategy")
        _require_enum(self.side, OrderSide, "side")
        _require_enum(self.order_type, OrderType, "order_type")
        _require_enum(self.status, OrderStatus, "status")
        _require_aware_datetime(self.created_at, "created_at")
        _require_uuid(self.order_id, "order_id")
        if self.signal_id is not None:
            _require_uuid(self.signal_id, "signal_id")
        if self.risk_decision_id is not None:
            _require_uuid(self.risk_decision_id, "risk_decision_id")
        _require_positive_integer(self.quantity, "quantity")
        if isinstance(self.filled_quantity, bool) or not isinstance(
            self.filled_quantity, int
        ):
            raise TypeError("filled_quantity must be an integer")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError(
                "filled_quantity must be between zero and quantity")
        _require_optional_price(self.limit_price, "limit_price")
        _require_optional_price(self.stop_price, "stop_price")
        _require_optional_price(self.average_fill_price, "average_fill_price")

        needs_limit = self.order_type in (
            OrderType.LIMIT, OrderType.STOP_LIMIT)
        needs_stop = self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
        if needs_limit != (self.limit_price is not None):
            raise ValueError(
                "limit_price must be provided only for limit order types")
        if needs_stop != (self.stop_price is not None):
            raise ValueError(
                "stop_price must be provided only for stop order types")
        if self.filled_quantity > 0 and self.average_fill_price is None:
            raise ValueError(
                "average_fill_price is required when quantity is filled")
        if self.status is OrderStatus.PARTIALLY_FILLED:
            if not 0 < self.filled_quantity < self.quantity:
                raise ValueError(
                    "PARTIALLY_FILLED orders require a partial filled_quantity"
                )
        if self.status is OrderStatus.FILLED and self.filled_quantity != self.quantity:
            raise ValueError(
                "FILLED orders must have filled_quantity equal to quantity")


@dataclass(frozen=True, slots=True)
class Position:
    """An open position snapshot in one instrument."""

    instrument_id: str
    symbol: str
    side: PositionSide
    quantity: int
    average_entry_price: float
    opened_at: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    position_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.symbol, "symbol")
        _require_enum(self.side, PositionSide, "side")
        _require_positive_integer(self.quantity, "quantity")
        _require_positive_number(
            self.average_entry_price, "average_entry_price")
        _require_aware_datetime(self.opened_at, "opened_at")
        _require_uuid(self.position_id, "position_id")
        _require_optional_price(self.stop_loss, "stop_loss")
        _require_optional_price(self.take_profit, "take_profit")
        if self.side is PositionSide.LONG:
            if self.stop_loss is not None and self.stop_loss >= self.average_entry_price:
                raise ValueError(
                    "LONG stop_loss must be below average_entry_price")
            if self.take_profit is not None and self.take_profit <= self.average_entry_price:
                raise ValueError(
                    "LONG take_profit must be above average_entry_price")
        else:
            if self.stop_loss is not None and self.stop_loss <= self.average_entry_price:
                raise ValueError(
                    "SHORT stop_loss must be above average_entry_price")
            if self.take_profit is not None and self.take_profit >= self.average_entry_price:
                raise ValueError(
                    "SHORT take_profit must be below average_entry_price")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """An explicit approval or rejection from a future risk-check boundary."""

    status: RiskDecisionStatus
    timestamp: datetime
    reason: str | None = None
    signal_id: UUID | None = None
    order_id: UUID | None = None
    decision_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_enum(self.status, RiskDecisionStatus, "status")
        _require_aware_datetime(self.timestamp, "timestamp")
        _require_uuid(self.decision_id, "decision_id")
        if self.signal_id is not None:
            _require_uuid(self.signal_id, "signal_id")
        if self.order_id is not None:
            _require_uuid(self.order_id, "order_id")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")
        if self.status is RiskDecisionStatus.REJECTED and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError(
                "rejected risk decisions require a non-empty reason")
