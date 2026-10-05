"""Paper-only fill simulation and in-memory portfolio accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from uuid import UUID, uuid4

from .models import (
    Instrument,
    Order,
    OrderSide,
    Position,
    PositionSide,
    RiskDecision,
    RiskDecisionStatus,
)
from .risk import OrderIntent


class PaperAccountingMode(str, Enum):
    MARGIN_LONG_ONLY = "MARGIN_LONG_ONLY"
    CASH_LONG_SHORT = "CASH_LONG_SHORT"


class PaperAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"
    COVER = "COVER"


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    accounting_mode: PaperAccountingMode = PaperAccountingMode.MARGIN_LONG_ONLY
    margin_rate: float = 0.20
    brokerage_rate: float = 0.0003
    brokerage_cap: float = 20.0
    exchange_rate: float = 0.0000325
    sebi_rate: float = 0.000001
    gst_rate: float = 0.18
    stamp_rate: float = 0.00003
    stt_rate: float = 0.00025
    allow_averaging: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.accounting_mode, PaperAccountingMode):
            raise TypeError("accounting_mode must be a PaperAccountingMode")
        for name in (
            "margin_rate",
            "brokerage_rate",
            "brokerage_cap",
            "exchange_rate",
            "sebi_rate",
            "gst_rate",
            "stamp_rate",
            "stt_rate",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not isinstance(self.allow_averaging, bool):
            raise TypeError("allow_averaging must be a bool")


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: UUID
    order_id: UUID
    instrument_id: str
    symbol: str
    action: PaperAction
    quantity: int
    price: float
    filled_at: datetime
    charges: float
    realized_pnl: float
    cash_after: float
    margin_delta: float
    note: str = ""


@dataclass(slots=True)
class PaperPortfolio:
    """Mutable in-memory paper state; persistence remains the caller's concern."""

    starting_cash: float
    cash: float | None = None
    realized_pnl: float = 0.0
    total_charges: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    margin_used: dict[str, float] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_finite_non_negative(self.starting_cash, "starting_cash")
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be greater than zero")
        if self.cash is None:
            self.cash = float(self.starting_cash)
        _require_finite_non_negative(self.cash, "cash")
        _require_finite_number(self.realized_pnl, "realized_pnl")
        _require_finite_non_negative(self.total_charges, "total_charges")
        if not isinstance(self.positions, dict) or not isinstance(self.margin_used, dict):
            raise TypeError("positions and margin_used must be dictionaries")
        for key, position in self.positions.items():
            if not isinstance(position, Position) or key != position.instrument_id:
                raise ValueError(
                    "positions must be keyed by matching instrument_id")
        for key, margin in self.margin_used.items():
            _require_finite_non_negative(margin, f"margin_used[{key}]")
            if key not in self.positions:
                raise ValueError(
                    "margin cannot be recorded without an open position")


class PaperExecutionError(ValueError):
    """A paper fill could not be applied without violating account invariants."""


def estimate_paper_charges(
    action: PaperAction,
    turnover: float,
    policy: PaperExecutionPolicy | None = None,
) -> float:
    """Estimate fees using the legacy dashboard's charge schedule."""
    policy = policy or PaperExecutionPolicy()
    _require_finite_positive(turnover, "turnover")
    brokerage = min(turnover * policy.brokerage_rate, policy.brokerage_cap)
    exchange = turnover * policy.exchange_rate
    sebi = turnover * policy.sebi_rate
    gst = policy.gst_rate * (brokerage + exchange + sebi)
    stamp = turnover * policy.stamp_rate if action is PaperAction.BUY else 0.0
    stt = turnover * policy.stt_rate if action is PaperAction.SELL else 0.0
    return brokerage + exchange + sebi + gst + stamp + stt


class PaperExecutor:
    """Simulate fills only. This class has no provider, broker, or network path."""

    def __init__(self, policy: PaperExecutionPolicy | None = None) -> None:
        self.policy = policy or PaperExecutionPolicy()

    def execute(
        self,
        order: Order,
        instrument: Instrument,
        portfolio: PaperPortfolio,
        *,
        risk_decision: RiskDecision,
        fill_price: float,
        intent: OrderIntent,
        filled_at: datetime | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        note: str = "",
    ) -> PaperFill:
        """Apply one full simulated fill and atomically update the paper account."""
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(instrument, Instrument):
            raise TypeError("instrument must be an Instrument")
        if not isinstance(portfolio, PaperPortfolio):
            raise TypeError("portfolio must be a PaperPortfolio")
        if not isinstance(risk_decision, RiskDecision):
            raise TypeError("risk_decision must be a RiskDecision")
        if (
            risk_decision.status is not RiskDecisionStatus.APPROVED
            or risk_decision.order_id != order.order_id
        ):
            raise PaperExecutionError(
                "Paper execution requires an approved risk decision for this order"
            )
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        if order.instrument_id != instrument.instrument_id or order.symbol != instrument.symbol:
            raise PaperExecutionError(
                "Order and instrument identities do not match")
        _require_finite_positive(fill_price, "fill_price")
        fill_time = filled_at or datetime.now(timezone.utc)
        if not _is_aware(fill_time):
            raise PaperExecutionError("filled_at must be timezone-aware")
        if not isinstance(note, str):
            raise TypeError("note must be a string")

        current = portfolio.positions.get(instrument.instrument_id)
        if intent is OrderIntent.ENTRY:
            if current is not None and current.side is not _entry_position_side(order.side):
                raise PaperExecutionError(
                    "Cannot add an entry opposite to an open position")
            if current is not None and not self.policy.allow_averaging:
                raise PaperExecutionError(
                    "Averaging is disabled for this paper policy")
            action = _entry_action(order.side)
            new_position, realized, margin_delta, cash_delta = self._apply_entry(
                order,
                instrument,
                current,
                portfolio,
                action,
                fill_price,
                stop_loss,
                take_profit,
            )
        else:
            if current is None:
                raise PaperExecutionError(
                    "No open position exists for this exit")
            if order.quantity > current.quantity:
                raise PaperExecutionError(
                    "Exit quantity exceeds the open position")
            if current.side is PositionSide.LONG and order.side is not OrderSide.SELL:
                raise PaperExecutionError(
                    "A long position must be exited with SELL")
            if current.side is PositionSide.SHORT and order.side is not OrderSide.BUY:
                raise PaperExecutionError(
                    "A short position must be exited with BUY")
            action = _exit_action(current.side)
            new_position, realized, margin_delta, cash_delta = self._apply_exit(
                order,
                current,
                portfolio,
                action,
                fill_price,
            )

        notional = fill_price * order.quantity
        charges = estimate_paper_charges(action, notional, self.policy)
        cash_after = float(portfolio.cash) + cash_delta
        if not isfinite(cash_after) or cash_after < 0:
            raise PaperExecutionError(
                f"Paper cash would become invalid or negative ({cash_after:.2f})"
            )
        current_margin = float(portfolio.margin_used.get(
            instrument.instrument_id, 0.0))
        new_margin = current_margin + margin_delta
        if new_margin < -1e-8 or not isfinite(new_margin):
            raise PaperExecutionError(
                "Paper margin state would become invalid")

        fill = PaperFill(
            fill_id=uuid4(),
            order_id=order.order_id,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            action=action,
            quantity=order.quantity,
            price=float(fill_price),
            filled_at=fill_time,
            charges=charges,
            realized_pnl=realized,
            cash_after=cash_after,
            margin_delta=margin_delta,
            note=note.strip(),
        )

        if new_position is None:
            portfolio.positions.pop(instrument.instrument_id, None)
            portfolio.margin_used.pop(instrument.instrument_id, None)
        else:
            portfolio.positions[instrument.instrument_id] = new_position
            if new_margin > 0:
                portfolio.margin_used[instrument.instrument_id] = new_margin
            else:
                portfolio.margin_used.pop(instrument.instrument_id, None)
        portfolio.cash = cash_after
        portfolio.realized_pnl += realized
        portfolio.total_charges += charges
        portfolio.fills.append(fill)
        return fill

    def _apply_entry(
        self,
        order: Order,
        instrument: Instrument,
        current: Position | None,
        portfolio: PaperPortfolio,
        action: PaperAction,
        price: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> tuple[Position, float, float, float]:
        quantity = order.quantity
        old_quantity = current.quantity if current is not None else 0
        old_average = current.average_entry_price if current is not None else 0.0
        resulting_quantity = old_quantity + quantity
        average_price = (
            old_quantity * old_average + quantity * price
        ) / resulting_quantity
        side = _entry_position_side(order.side)

        if side is PositionSide.LONG:
            if self.policy.accounting_mode is PaperAccountingMode.MARGIN_LONG_ONLY:
                if current is not None:
                    raise PaperExecutionError(
                        "The margin-long paper policy does not permit averaging"
                    )
                margin_delta = quantity * price * self.policy.margin_rate
                charges = estimate_paper_charges(
                    action, quantity * price, self.policy)
                cash_delta = -(margin_delta + charges)
            else:
                margin_delta = 0.0
                charges = estimate_paper_charges(
                    action, quantity * price, self.policy)
                cash_delta = -(quantity * price + charges)
            default_stop = average_price * 0.992
            default_target = average_price * 1.016
            stop = stop_loss if stop_loss is not None else (
                current.stop_loss if current is not None else default_stop
            )
            target = take_profit if take_profit is not None else (
                current.take_profit if current is not None else default_target
            )
        else:
            if self.policy.accounting_mode is not PaperAccountingMode.CASH_LONG_SHORT:
                raise PaperExecutionError(
                    "Short entry is unavailable under the margin-long paper policy"
                )
            margin_delta = 0.0
            charges = estimate_paper_charges(
                action, quantity * price, self.policy)
            cash_delta = quantity * price - charges
            default_stop = average_price * 1.008
            default_target = average_price * 0.984
            stop = stop_loss if stop_loss is not None else (
                current.stop_loss if current is not None else default_stop
            )
            target = take_profit if take_profit is not None else (
                current.take_profit if current is not None else default_target
            )

        position = Position(
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            side=side,
            quantity=resulting_quantity,
            average_entry_price=average_price,
            opened_at=current.opened_at if current is not None else order.created_at,
            stop_loss=stop,
            take_profit=target,
            position_id=current.position_id if current is not None else uuid4(),
        )
        return position, 0.0, margin_delta, cash_delta

    def _apply_exit(
        self,
        order: Order,
        current: Position,
        portfolio: PaperPortfolio,
        action: PaperAction,
        price: float,
    ) -> tuple[Position | None, float, float, float]:
        quantity = order.quantity
        notional = quantity * price
        charges = estimate_paper_charges(action, notional, self.policy)
        margin_used = float(portfolio.margin_used.get(
            current.instrument_id, 0.0))

        if current.side is PositionSide.LONG:
            realized = (price - current.average_entry_price) * quantity
            if self.policy.accounting_mode is PaperAccountingMode.MARGIN_LONG_ONLY:
                margin_release = current.average_entry_price * quantity * self.policy.margin_rate
                cash_delta = margin_release + realized - charges
                margin_delta = -margin_release
            else:
                cash_delta = notional - charges
                margin_delta = 0.0
        else:
            if self.policy.accounting_mode is not PaperAccountingMode.CASH_LONG_SHORT:
                raise PaperExecutionError(
                    "Short exit is unavailable under the margin-long paper policy"
                )
            realized = (current.average_entry_price - price) * quantity
            cash_delta = -(notional + charges)
            margin_delta = 0.0

        remaining = current.quantity - quantity
        if remaining == 0:
            return None, realized, margin_delta, cash_delta
        remaining_margin_delta = margin_delta
        if current.side is PositionSide.LONG and self.policy.accounting_mode is PaperAccountingMode.MARGIN_LONG_ONLY:
            expected_remaining_margin = current.average_entry_price * \
                remaining * self.policy.margin_rate
            remaining_margin_delta = expected_remaining_margin - margin_used
        updated = Position(
            instrument_id=current.instrument_id,
            symbol=current.symbol,
            side=current.side,
            quantity=remaining,
            average_entry_price=current.average_entry_price,
            opened_at=current.opened_at,
            stop_loss=current.stop_loss,
            take_profit=current.take_profit,
            position_id=current.position_id,
        )
        return updated, realized, remaining_margin_delta, cash_delta


def _entry_action(side: OrderSide) -> PaperAction:
    return PaperAction.BUY if side is OrderSide.BUY else PaperAction.SHORT


def _entry_position_side(side: OrderSide) -> PositionSide:
    return PositionSide.LONG if side is OrderSide.BUY else PositionSide.SHORT


def _exit_action(side: PositionSide) -> PaperAction:
    return PaperAction.SELL if side is PositionSide.LONG else PaperAction.COVER


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_finite_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _require_finite_positive(value: float, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
