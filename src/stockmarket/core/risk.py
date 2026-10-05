"""Standalone pre-trade risk evaluation for paper-trading workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from math import isclose, isfinite
from typing import Any
from uuid import UUID

from .models import (
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RiskDecision,
    RiskDecisionStatus,
    Signal,
    SignalSide,
    TradingStatus,
)
from .market_session import MarketSession
from .risk_portfolio import PortfolioRiskLimits, check_portfolio_limits


class OrderIntent(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Explicit entry limits; values must be configured for the target market."""

    max_position_quantity: int
    max_order_notional: float
    max_open_positions: int
    max_trades_per_day: int
    cash_requirement_rate: float
    entry_window: tuple[time, time] | None = None
    minimum_reward_risk: float | None = None
    minimum_expected_edge: float | None = None
    require_stop_loss: bool = True
    require_take_profit: bool = True
    max_market_data_age: timedelta = field(default=timedelta(minutes=5))
    market_session: MarketSession | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_position_quantity",
            "max_open_positions",
            "max_trades_per_day",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        _require_positive_number(self.max_order_notional, "max_order_notional")
        _require_positive_number(
            self.cash_requirement_rate, "cash_requirement_rate")
        if self.minimum_reward_risk is not None:
            _require_non_negative_number(
                self.minimum_reward_risk, "minimum_reward_risk"
            )
        if self.minimum_expected_edge is not None:
            _require_finite_number(
                self.minimum_expected_edge, "minimum_expected_edge"
            )
        if not isinstance(self.require_stop_loss, bool):
            raise TypeError("require_stop_loss must be a bool")
        if not isinstance(self.require_take_profit, bool):
            raise TypeError("require_take_profit must be a bool")
        if not isinstance(self.max_market_data_age, timedelta):
            raise TypeError("max_market_data_age must be a timedelta")
        if self.max_market_data_age <= timedelta(0):
            raise ValueError("max_market_data_age must be greater than zero")
        if self.entry_window is not None:
            if (
                not isinstance(self.entry_window, tuple)
                or len(self.entry_window) != 2
                or any(not isinstance(value, time) for value in self.entry_window)
                or any(value.tzinfo is not None for value in self.entry_window)
            ):
                raise ValueError(
                    "entry_window must be a pair of timezone-naive local times"
                )
        if self.market_session is not None and not isinstance(
            self.market_session, MarketSession
        ):
            raise TypeError("market_session must be a MarketSession or None")


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Snapshot of state/data needed to make one reproducible risk decision."""

    assessed_at: datetime | None
    market_data_timestamp: datetime | None
    market_data_valid: bool | None
    reference_price: Any
    available_cash: Any
    estimated_fees: Any
    current_position_quantity: Any
    current_position_side: PositionSide | None
    open_positions: Any
    trades_today: Any
    intent: OrderIntent = OrderIntent.ENTRY
    stop_loss: Any = None
    take_profit: Any = None
    signal: Signal | None = None
    portfolio: Any = None  # PortfolioRiskState; required when portfolio limits are set


def _is_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(value)
    )


def _require_finite_number(value: object, field_name: str) -> None:
    if not _is_finite_number(value):
        raise TypeError(f"{field_name} must be a finite number")


def _require_positive_number(value: object, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value <= 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be greater than zero")


def _require_non_negative_number(value: object, field_name: str) -> None:
    _require_finite_number(value, field_name)
    if value < 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be non-negative")


def _valid_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class RiskEngine:
    """Evaluate an order proposal before any portfolio/order state is mutated."""

    def __init__(
        self,
        limits: RiskLimits | None,
        portfolio_limits: PortfolioRiskLimits | None = None,
    ) -> None:
        if portfolio_limits is not None and not isinstance(
            portfolio_limits, PortfolioRiskLimits
        ):
            raise TypeError("portfolio_limits must be a PortfolioRiskLimits")
        self._limits = limits
        self._portfolio_limits = portfolio_limits

    @property
    def disabled_controls(self) -> dict[str, str]:
        """Explicitly disabled portfolio controls and their recorded reasons."""
        if self._portfolio_limits is None:
            return {}
        return dict(self._portfolio_limits.disabled)

    def evaluate_proposal(
        self,
        instrument: Instrument | None,
        *,
        side: object,
        quantity: object,
        order_type: object,
        created_at: object,
        context: RiskContext | None,
        limit_price: object = None,
        stop_price: object = None,
        strategy: str = "",
        signal_id: object = None,
    ) -> RiskDecision:
        """Validate raw order fields and return a decision instead of raising."""
        if self._limits is None:
            return self._reject(
                "RISK_LIMITS_MISSING",
                "Risk limits are not configured; the order cannot be evaluated.",
                context=context,
            )
        if instrument is None:
            return self._reject(
                "INSTRUMENT_MISSING",
                "A matching instrument definition is required.",
                context=context,
            )
        try:
            order = Order(
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                side=side,  # type: ignore[arg-type]
                quantity=quantity,  # type: ignore[arg-type]
                order_type=order_type,  # type: ignore[arg-type]
                created_at=created_at,  # type: ignore[arg-type]
                limit_price=limit_price,  # type: ignore[arg-type]
                stop_price=stop_price,  # type: ignore[arg-type]
                strategy=strategy,
                signal_id=signal_id,  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            return self._reject(
                "INVALID_ORDER",
                f"Order fields are invalid: {exc}.",
                context=context,
            )
        return self.evaluate(order, instrument, context)

    def evaluate(
        self,
        order: Order | None,
        instrument: Instrument | None,
        context: RiskContext | None,
    ) -> RiskDecision:
        """Apply configured checks to a validated order and state snapshot."""
        if self._limits is None:
            return self._reject(
                "RISK_LIMITS_MISSING",
                "Risk limits are not configured; the order cannot be evaluated.",
                order=order,
                context=context,
            )
        if order is None:
            return self._reject(
                "ORDER_MISSING", "An order is required for risk evaluation.", context=context
            )
        if not isinstance(order, Order):
            return self._reject(
                "INVALID_ORDER", "The proposed order is not a valid Order model.",
                context=context,
            )
        if instrument is None or not isinstance(instrument, Instrument):
            return self._reject(
                "INSTRUMENT_MISSING",
                "A valid instrument definition is required.",
                order=order,
                context=context,
            )
        if context is None or not isinstance(context, RiskContext):
            return self._reject(
                "RISK_CONTEXT_MISSING",
                "Portfolio, market-data, and assessment context are required.",
                order=order,
            )
        if order.status is not OrderStatus.NEW:
            return self._reject(
                "ORDER_NOT_NEW",
                "Only a new, unsubmitted order can be evaluated.",
                order,
                context,
            )
        if order.instrument_id != instrument.instrument_id or order.symbol != instrument.symbol:
            return self._reject(
                "INSTRUMENT_MISMATCH",
                "Order instrument identity does not match the supplied instrument.",
                order,
                context,
            )
        if not _is_aware(context.assessed_at):
            return self._reject(
                "ASSESSMENT_TIME_INVALID",
                "A timezone-aware assessment timestamp is required.",
                order,
                context,
            )
        if not _is_aware(order.created_at):
            return self._reject(
                "ORDER_TIME_INVALID",
                "The order timestamp must be timezone-aware.",
                order,
                context,
            )
        if order.created_at > context.assessed_at:
            return self._reject(
                "ORDER_TIME_IN_FUTURE",
                "The order timestamp is later than the assessment time.",
                order,
                context,
            )
        if not isinstance(context.intent, OrderIntent):
            return self._reject(
                "ORDER_INTENT_INVALID",
                "Order intent must be ENTRY or EXIT.",
                order,
                context,
            )
        if not _valid_count(order.quantity) or order.quantity == 0:
            return self._reject(
                "QUANTITY_INVALID",
                "Order quantity must be a positive integer.",
                order,
                context,
            )
        if context.market_data_valid is not True:
            return self._reject(
                "MARKET_DATA_INVALID",
                "Market data is missing or marked invalid; no order is approved.",
                order,
                context,
            )
        if not _is_aware(context.market_data_timestamp):
            return self._reject(
                "MARKET_DATA_TIMESTAMP_INVALID",
                "A timezone-aware market-data timestamp is required.",
                order,
                context,
            )
        if context.market_data_timestamp > context.assessed_at:
            return self._reject(
                "MARKET_DATA_FROM_FUTURE",
                "Market data is timestamped after the assessment time.",
                order,
                context,
            )
        data_age = context.assessed_at - context.market_data_timestamp
        if data_age > self._limits.max_market_data_age:
            return self._reject(
                "MARKET_DATA_STALE",
                "Market data is older than the configured freshness limit.",
                order,
                context,
            )
        if not _is_finite_number(context.reference_price) or context.reference_price <= 0:
            return self._reject(
                "REFERENCE_PRICE_INVALID",
                "A finite, positive reference price is required.",
                order,
                context,
            )
        if not _valid_count(context.current_position_quantity):
            return self._reject(
                "POSITION_QUANTITY_INVALID",
                "Current position quantity must be a non-negative integer.",
                order,
                context,
            )
        if not _valid_count(context.open_positions):
            return self._reject(
                "OPEN_POSITION_COUNT_INVALID",
                "Current open-position count must be a non-negative integer.",
                order,
                context,
            )
        has_position = context.current_position_quantity > 0
        if has_position and (
            not isinstance(context.current_position_side, PositionSide)
            or context.open_positions == 0
        ):
            return self._reject(
                "POSITION_STATE_CONTRADICTORY",
                "An existing position requires a side and a positive open-position count.",
                order,
                context,
            )
        if not has_position and context.current_position_side is not None:
            return self._reject(
                "POSITION_STATE_CONTRADICTORY",
                "A position side was supplied while current position quantity is zero.",
                order,
                context,
            )
        if context.signal is not None and not isinstance(context.signal, Signal):
            return self._reject(
                "SIGNAL_INVALID", "Signal context is not a valid Signal model.",
                order,
                context,
            )
        if context.signal is not None:
            signal = context.signal
            if signal.instrument_id != instrument.instrument_id:
                return self._reject(
                    "SIGNAL_INSTRUMENT_MISMATCH",
                    "Signal instrument does not match the proposed order.",
                    order,
                    context,
                )
            if order.signal_id is not None and order.signal_id != signal.signal_id:
                return self._reject(
                    "SIGNAL_ID_MISMATCH",
                    "Order signal identifier does not match the supplied signal.",
                    order,
                    context,
                )
            if signal.timestamp > context.assessed_at:
                return self._reject(
                    "SIGNAL_FROM_FUTURE",
                    "Signal timestamp is later than the assessment time.",
                    order,
                    context,
                )
            if context.market_data_timestamp < signal.timestamp:
                return self._reject(
                    "SIGNAL_DATA_MISMATCH",
                    "Signal is newer than the market-data snapshot being assessed.",
                    order,
                    context,
                )

        if context.intent is OrderIntent.EXIT:
            if not has_position or order.quantity > context.current_position_quantity:
                return self._reject(
                    "EXIT_QUANTITY_EXCEEDS_POSITION",
                    "Exit quantity must be positive and no greater than the open position.",
                    order,
                    context,
                )
            expected_position_side = (
                PositionSide.SHORT
                if order.side is OrderSide.BUY
                else PositionSide.LONG
            )
            if context.current_position_side is not expected_position_side:
                return self._reject(
                    "EXIT_SIDE_CONTRADICTS_POSITION",
                    "Exit side does not reduce the current position direction.",
                    order,
                    context,
                )
        else:
            entry_rejection = self._evaluate_entry(order, instrument, context)
            if entry_rejection is not None:
                return entry_rejection

        return self._decision(
            RiskDecisionStatus.APPROVED,
            "APPROVED: all applicable configured risk checks passed.",
            order,
            context,
        )

    def _evaluate_entry(
        self,
        order: Order,
        instrument: Instrument,
        context: RiskContext,
    ) -> RiskDecision | None:
        assert self._limits is not None
        if instrument.trading_status is not TradingStatus.ACTIVE:
            return self._reject(
                "INSTRUMENT_NOT_ACTIVE",
                "New entries are allowed only for instruments marked ACTIVE.",
                order,
                context,
            )
        if order.side is OrderSide.SELL and instrument.shortable is not True:
            return self._reject(
                "SHORTABILITY_UNCONFIRMED",
                "A sell entry requires the instrument to be explicitly marked shortable.",
                order,
                context,
            )
        expected_side = (
            PositionSide.LONG if order.side is OrderSide.BUY else PositionSide.SHORT
        )
        if context.current_position_quantity > 0 and context.current_position_side is not expected_side:
            return self._reject(
                "POSITION_DIRECTION_CONTRADICTORY",
                "An entry cannot add exposure in the opposite direction of the open position.",
                order,
                context,
            )
        resulting_quantity = context.current_position_quantity + order.quantity
        if resulting_quantity > self._limits.max_position_quantity:
            return self._reject(
                "MAX_POSITION_QUANTITY_EXCEEDED",
                f"Resulting position quantity {resulting_quantity} exceeds the configured limit {self._limits.max_position_quantity}.",
                order,
                context,
            )
        if context.open_positions >= self._limits.max_open_positions and not (
            context.current_position_quantity > 0
        ):
            return self._reject(
                "MAX_OPEN_POSITIONS_EXCEEDED",
                f"Open-position count {context.open_positions} has reached the configured limit {self._limits.max_open_positions}.",
                order,
                context,
            )
        if not _valid_count(context.trades_today):
            return self._reject(
                "DAILY_TRADE_COUNT_INVALID",
                "Trades-today count must be a non-negative integer.",
                order,
                context,
            )
        if context.trades_today >= self._limits.max_trades_per_day:
            return self._reject(
                "MAX_TRADES_PER_DAY_EXCEEDED",
                f"Trades today {context.trades_today} has reached the configured limit {self._limits.max_trades_per_day}.",
                order,
                context,
            )
        if self._limits.market_session is None and self._limits.entry_window is None:
            return self._reject(
                "ENTRY_WINDOW_UNCONFIGURED",
                "An instrument-local entry window must be configured for new entries.",
                order,
                context,
            )
        session = self._limits.market_session
        in_window = (
            session.is_entry_allowed(context.assessed_at)
            if session is not None
            else MarketSession(
                timezone=instrument.timezone,
                market_open=self._limits.entry_window[0],
                opening_range_end=self._limits.entry_window[0],
                entry_cutoff=self._limits.entry_window[1],
                square_off=self._limits.entry_window[1],
                market_close=self._limits.entry_window[1],
                entry_start=self._limits.entry_window[0],
                late_entry_start=self._limits.entry_window[0],
            ).is_between_local_times(
                context.assessed_at,
                self._limits.entry_window[0],
                self._limits.entry_window[1],
            )
        )
        if not in_window:
            return self._reject(
                "OUTSIDE_ENTRY_WINDOW",
                "Assessment time falls outside the configured instrument-local entry window.",
                order,
                context,
            )

        try:
            _require_non_negative_number(
                context.available_cash, "available_cash")
            _require_non_negative_number(
                context.estimated_fees, "estimated_fees")
        except (TypeError, ValueError) as exc:
            return self._reject(
                "CASH_CONTEXT_INVALID",
                f"Available cash and estimated fees must be valid: {exc}.",
                order,
                context,
            )

        protective = self._protective_levels(context, order)
        if isinstance(protective, RiskDecision):
            return protective
        stop_loss, take_profit = protective
        if self._limits.require_stop_loss and stop_loss is None:
            return self._reject(
                "STOP_LOSS_MISSING",
                "A stop-loss level is required for a new position.",
                order,
                context,
            )
        if self._limits.require_take_profit and take_profit is None:
            return self._reject(
                "TAKE_PROFIT_MISSING",
                "A take-profit level is required for a new position.",
                order,
                context,
            )
        if stop_loss is not None and take_profit is not None:
            reference_price = float(context.reference_price)
            if order.side is OrderSide.BUY:
                risk_per_unit = reference_price - stop_loss
                reward_per_unit = take_profit - reference_price
            else:
                risk_per_unit = stop_loss - reference_price
                reward_per_unit = reference_price - take_profit
            if risk_per_unit <= 0 or reward_per_unit <= 0:
                return self._reject(
                    "PROTECTIVE_LEVELS_INVALID",
                    "Stop-loss and take-profit must be on the correct sides of the reference price.",
                    order,
                    context,
                )
            if self._limits.minimum_reward_risk is not None:
                reward_risk = reward_per_unit / risk_per_unit
                if reward_risk < self._limits.minimum_reward_risk:
                    return self._reject(
                        "MINIMUM_REWARD_RISK_NOT_MET",
                        f"Reward/risk {reward_risk:.3f} is below the configured minimum {self._limits.minimum_reward_risk:.3f}.",
                        order,
                        context,
                    )
        elif self._limits.minimum_reward_risk is not None:
            return self._reject(
                "REWARD_RISK_DATA_MISSING",
                "Stop-loss and take-profit are required to calculate reward/risk.",
                order,
                context,
            )

        if self._limits.minimum_expected_edge is not None:
            if context.signal is None:
                return self._reject(
                    "EXPECTED_EDGE_DATA_MISSING",
                    "A validated signal is required by the configured expected-edge gate.",
                    order,
                    context,
                )
            if context.signal.expected_edge < self._limits.minimum_expected_edge:
                return self._reject(
                    "MINIMUM_EXPECTED_EDGE_NOT_MET",
                    f"Expected edge {context.signal.expected_edge:.2f} is below the configured minimum {self._limits.minimum_expected_edge:.2f}.",
                    order,
                    context,
                )

        notional_price = max(
            float(context.reference_price),
            float(order.limit_price or 0.0),
            float(order.stop_price or 0.0),
        )
        notional = notional_price * order.quantity
        if notional > self._limits.max_order_notional:
            return self._reject(
                "MAX_ORDER_NOTIONAL_EXCEEDED",
                f"Order notional {notional:.2f} exceeds the configured limit {self._limits.max_order_notional:.2f}.",
                order,
                context,
            )
        required_cash = (
            notional * self._limits.cash_requirement_rate
            + float(context.estimated_fees)
        )
        if float(context.available_cash) < required_cash:
            return self._reject(
                "INSUFFICIENT_CASH_OR_MARGIN",
                f"Required cash/margin {required_cash:.2f} exceeds available cash {float(context.available_cash):.2f}.",
                order,
                context,
            )
        if self._portfolio_limits is not None:
            breach = check_portfolio_limits(
                self._portfolio_limits,
                context.portfolio,
                order,
                reference_price=float(context.reference_price),
                notional=notional,
                stop_loss=stop_loss,
            )
            if breach is not None:
                return self._reject(breach[0], breach[1], order, context)
        return None

    def _protective_levels(
        self, context: RiskContext, order: Order
    ) -> tuple[float | None, float | None] | RiskDecision:
        assert self._limits is not None
        signal = context.signal
        signal_stop = signal.stop_loss if signal is not None else None
        signal_target = signal.take_profit if signal is not None else None
        for name, contextual, signaled in (
            ("stop_loss", context.stop_loss, signal_stop),
            ("take_profit", context.take_profit, signal_target),
        ):
            if contextual is not None and signaled is not None and not isclose(
                contextual, signaled, rel_tol=1e-9, abs_tol=1e-9
            ):
                return self._reject(
                    "PROTECTIVE_LEVELS_CONTRADICTORY",
                    f"Context and signal provide conflicting {name} values.",
                    order,
                    context=context,
                )
        stop_loss = context.stop_loss if context.stop_loss is not None else signal_stop
        take_profit = (
            context.take_profit
            if context.take_profit is not None
            else signal_target
        )
        try:
            if stop_loss is not None:
                _require_positive_number(stop_loss, "stop_loss")
            if take_profit is not None:
                _require_positive_number(take_profit, "take_profit")
        except (TypeError, ValueError) as exc:
            return self._reject(
                "PROTECTIVE_LEVELS_INVALID",
                f"Protective price levels must be finite and positive: {exc}.",
                order,
                context=context,
            )
        return (
            float(stop_loss) if stop_loss is not None else None,
            float(take_profit) if take_profit is not None else None,
        )

    def _reject(
        self,
        code: str,
        explanation: str,
        order: Order | None = None,
        context: RiskContext | None = None,
    ) -> RiskDecision:
        return self._decision(
            RiskDecisionStatus.REJECTED,
            f"{code}: {explanation}",
            order,
            context,
        )

    @staticmethod
    def _decision(
        status: RiskDecisionStatus,
        reason: str,
        order: Order | None,
        context: RiskContext | None,
    ) -> RiskDecision:
        timestamp = (
            context.assessed_at
            if context is not None and _is_aware(context.assessed_at)
            else order.created_at
            if order is not None and _is_aware(order.created_at)
            else datetime.now(timezone.utc)
        )
        return RiskDecision(
            status=status,
            timestamp=timestamp,
            reason=reason,
            order_id=order.order_id if isinstance(order, Order) else None,
            signal_id=(
                order.signal_id if isinstance(order, Order) else None
            ),
        )
