"""Risk-based position sizing: quantity = risk budget / stop distance, then capped."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, isfinite
from typing import Mapping

from .models import Instrument, OrderSide


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _positive(value: object, name: str) -> None:
    if not _finite(value) or value <= 0:  # type: ignore[operator]
        raise ValueError(f"{name} must be a finite number greater than zero")


def _optional_positive(value: object, name: str) -> None:
    if value is not None:
        _positive(value, name)


@dataclass(frozen=True, slots=True)
class BrokerConstraints:
    max_order_quantity: int | None = None
    max_order_notional: float | None = None

    def __post_init__(self) -> None:
        _optional_positive(self.max_order_quantity, "max_order_quantity")
        _optional_positive(self.max_order_notional, "max_order_notional")


@dataclass(frozen=True, slots=True)
class SizingLimits:
    """Fractions are of equity; cash_requirement_rate is the cash share needed per unit notional."""

    risk_per_trade_pct: float
    max_order_notional: float
    cash_requirement_rate: float = 1.0
    max_position_notional_pct: float | None = None
    max_total_notional_pct: float | None = None
    max_sector_exposure_pct: float | None = None
    max_participation_rate: float | None = None  # share of average daily volume
    broker: BrokerConstraints = field(default_factory=BrokerConstraints)

    def __post_init__(self) -> None:
        _positive(self.risk_per_trade_pct, "risk_per_trade_pct")
        if self.risk_per_trade_pct > 1:
            raise ValueError("risk_per_trade_pct must not exceed 1")
        _positive(self.max_order_notional, "max_order_notional")
        _positive(self.cash_requirement_rate, "cash_requirement_rate")
        for name in ("max_position_notional_pct", "max_total_notional_pct",
                     "max_sector_exposure_pct", "max_participation_rate"):
            _optional_positive(getattr(self, name), name)
        if self.max_participation_rate is not None and self.max_participation_rate > 1:
            raise ValueError("max_participation_rate must not exceed 1")


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: int
    risk_budget: float
    stop_distance: float
    raw_quantity: float
    binding_constraint: str
    caps: Mapping[str, float]
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.quantity > 0


def _zero(reason: str, budget: float = 0.0, distance: float = 0.0) -> SizingResult:
    return SizingResult(0, budget, distance, 0.0, reason, {}, reason)


def size_position(
    instrument: Instrument,
    side: OrderSide,
    *,
    entry_price: float,
    stop_price: float,
    equity: float,
    available_cash: float,
    limits: SizingLimits,
    gross_exposure: float = 0.0,
    sector_exposure: float = 0.0,
    current_position_notional: float = 0.0,
    average_daily_volume: float | None = None,
) -> SizingResult:
    """Return the largest compliant quantity; quantity 0 carries the binding reason."""
    for name, value in (("entry_price", entry_price), ("stop_price", stop_price), ("equity", equity)):
        if not _finite(value) or value <= 0:
            return _zero(f"INVALID_{name.upper()}")
    for name, value in (("available_cash", available_cash), ("gross_exposure", gross_exposure),
                        ("sector_exposure", sector_exposure),
                        ("current_position_notional", current_position_notional)):
        if not _finite(value) or value < 0:
            return _zero(f"INVALID_{name.upper()}")

    stop_distance = entry_price - \
        stop_price if side is OrderSide.BUY else stop_price - entry_price
    if stop_distance <= 0:
        return _zero("STOP_ON_WRONG_SIDE_OF_ENTRY")

    risk_budget = equity * limits.risk_per_trade_pct
    raw = risk_budget / stop_distance

    caps: dict[str, float] = {
        "risk_budget": raw,
        "order_notional": limits.max_order_notional / entry_price,
        "account_cash": available_cash / (entry_price * limits.cash_requirement_rate),
    }
    if limits.max_position_notional_pct is not None:
        caps["position_notional"] = (
            limits.max_position_notional_pct * equity - current_position_notional) / entry_price
    if limits.max_total_notional_pct is not None:
        caps["portfolio_exposure"] = (
            limits.max_total_notional_pct * equity - gross_exposure) / entry_price
    if limits.max_sector_exposure_pct is not None:
        caps["sector_exposure"] = (
            limits.max_sector_exposure_pct * equity - sector_exposure) / entry_price
    if limits.max_participation_rate is not None:
        if average_daily_volume is None or not _finite(average_daily_volume) or average_daily_volume <= 0:
            return _zero("LIQUIDITY_UNKNOWN", risk_budget, stop_distance)
        caps["liquidity"] = limits.max_participation_rate * average_daily_volume
    if limits.broker.max_order_quantity is not None:
        caps["broker_quantity"] = float(limits.broker.max_order_quantity)
    if limits.broker.max_order_notional is not None:
        caps["broker_notional"] = limits.broker.max_order_notional / entry_price

    binding = min(caps, key=lambda k: caps[k])
    limit_qty = max(0.0, caps[binding])

    lot = instrument.lot_size
    quantity = int(floor(limit_qty / lot + 1e-9)) * lot
    if quantity < max(instrument.minimum_order_quantity, lot):
        reason = "BELOW_MINIMUM_ORDER_SIZE" if limit_qty > 0 else f"NO_CAPACITY_{binding.upper()}"
        return SizingResult(0, risk_budget, stop_distance, raw, binding, caps, reason)
    return SizingResult(quantity, risk_budget, stop_distance, raw, binding, caps)
