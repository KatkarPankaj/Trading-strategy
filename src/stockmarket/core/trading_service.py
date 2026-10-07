"""Order entry service: the single place where tickets become risk-checked, tracked orders."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import UUID

from .aggregation import AggregatedDecision
from .audit_trail import AuditContext, live_gaps
from .executors import TradingMode
from .models import (
    Instrument, OrderSide, OrderType, PositionSide, RiskDecision,
    RiskDecisionStatus, Signal, SignalSide,
)
from .order_management import (
    ManagedOrder,
    OrderManager,
    OrderRequest,
    SubmissionResult,
    new_client_order_id,
)
from .portfolio import PortfolioManager
from .risk import OrderIntent, RiskContext, RiskEngine
from .risk_portfolio import order_key_for
from .sizing import SizingLimits, SizingResult, size_position

QuoteSource = Callable[[str], "tuple[float, datetime] | None"]
MarketStats = Callable[[str], Mapping[str, Any]]


class UnknownInstrument(ValueError):
    pass


class AutomaticSizingRejected(ValueError):
    """A sizing prerequisite failed before an order could be submitted."""


@dataclass(frozen=True, slots=True)
class OrderTicket:
    instrument_id: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    strategy: str
    client_order_id: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    signal_id: UUID | None = None
    intent: OrderIntent = OrderIntent.ENTRY
    market_regime: str | None = None
    # hash of the parameters the strategy is actually running
    parameters_hash: str | None = None
    audit: AuditContext | None = None
    signal: Signal | None = None


@dataclass(frozen=True, slots=True)
class TicketResult:
    order: ManagedOrder
    risk: RiskDecision
    duplicate: bool


class TradingService:
    """Every order, from any caller, is risk-evaluated before the OrderManager can send it."""

    def __init__(
        self,
        *,
        mode: TradingMode,
        risk_engine: RiskEngine,
        order_manager: OrderManager,
        portfolio: PortfolioManager,
        instruments: Mapping[str, Instrument],
        quotes: QuoteSource,
        market_stats: MarketStats | None = None,
        sector_of: Callable[[str], str | None] | None = None,
        store: Any = None,
        clock: Callable[[], datetime] | None = None,
        gate: Any = None,
        strategy_approval: Callable[[str], str | None] | None = None,
        provenance_source: Callable[[str], Any] | None = None,
        sizing_limits: SizingLimits | None = None,
    ) -> None:
        self.mode = mode
        self.order_manager = order_manager
        self.portfolio = portfolio
        self.instruments = instruments
        self._risk = risk_engine
        self._quotes = quotes
        self._stats = market_stats or (lambda iid: {})
        self._sector_of = sector_of or (lambda iid: None)
        self._store = store
        self._gate = gate
        self._strategy_approval = strategy_approval
        self._provenance_source = provenance_source
        self._sizing_limits = sizing_limits
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._submit_lock = RLock()
        if store is not None:  # write-ahead: each transition is stored before the next step runs
            order_manager.on_change = self._persist_transition

    def submit(
        self,
        ticket: OrderTicket,
        *,
        actor: str = "system",
        strategy_decision: AggregatedDecision | None = None,
    ) -> TicketResult:
        with self._submit_lock:
            return self._submit_locked(
                ticket, actor=actor, strategy_decision=strategy_decision)

    def _submit_locked(
        self,
        ticket: OrderTicket,
        *,
        actor: str = "system",
        strategy_decision: AggregatedDecision | None = None,
    ) -> TicketResult:
        instrument = self.instruments.get(ticket.instrument_id)
        if instrument is None:
            raise UnknownInstrument(ticket.instrument_id)
        if ticket.signal is not None:
            if not isinstance(ticket.signal, Signal):
                raise TypeError("ticket.signal must be a Signal or None")
            if (ticket.signal.instrument_id != ticket.instrument_id
                    or ticket.signal.symbol != instrument.symbol
                    or ticket.signal.strategy != ticket.strategy
                    or ticket.signal.signal_id != ticket.signal_id
                    or ticket.signal.side.value != ticket.side.value):
                raise ValueError("ticket signal identity and side must match the order ticket")
        if strategy_decision is not None:
            if not isinstance(strategy_decision, AggregatedDecision):
                raise TypeError("strategy_decision must be an AggregatedDecision or None")
            if ticket.signal is None or (
                strategy_decision.instrument_id != ticket.instrument_id
                or strategy_decision.symbol != instrument.symbol
                or strategy_decision.strategy != ticket.strategy
                or strategy_decision.action.value != ticket.side.value
                or ticket.audit is None
                or ticket.audit.strategy_decision_id != str(strategy_decision.decision_id)
            ):
                raise ValueError(
                    "strategy decision identity and action must match the audited signal")
        now = self._clock()
        client_order_id = ticket.client_order_id or new_client_order_id()

        decision = self._halt_decision(ticket, now)
        market_data = None
        if decision is None:
            market_data = self._quotes(ticket.instrument_id)
            decision = self._risk.evaluate_proposal(
                instrument, side=ticket.side, quantity=ticket.quantity, order_type=ticket.order_type,
                created_at=now, context=self._context(ticket, instrument, now, market_data),
                limit_price=ticket.limit_price, stop_price=ticket.stop_price,
                strategy=ticket.strategy, signal_id=ticket.signal_id)
        request = OrderRequest(
            client_order_id=client_order_id, instrument_id=instrument.instrument_id,
            symbol=instrument.symbol, side=ticket.side, quantity=ticket.quantity,
            order_type=ticket.order_type, timestamp=now, strategy=ticket.strategy,
            signal_id=ticket.signal_id, limit_price=ticket.limit_price, stop_price=ticket.stop_price)
        self._record_provenance(client_order_id, ticket, now, market_data)
        self._record_audit(
            client_order_id, ticket, now, decision, market_data, strategy_decision)
        result = self.order_manager.submit(request, decision)
        if not result.duplicate and not result.order.is_terminal:
            result = SubmissionResult(self.order_manager.sync(client_order_id))
        self._record_broker_response(result.order)
        self._persist(result, decision, actor, "ORDER_SUBMITTED")
        return TicketResult(result.order, decision, result.duplicate)

    def submit_sized_signal(
        self,
        signal: Signal,
        *,
        actor: str = "system",
        audit: AuditContext | None = None,
        strategy_decision: AggregatedDecision | None = None,
        client_order_id: str | None = None,
    ) -> tuple[TicketResult, SizingResult]:
        """Size from a fresh quote, then run the ordinary final RiskEngine gate."""
        if self.mode is not TradingMode.PAPER:
            raise RuntimeError("automatic signal sizing is restricted to PAPER mode")
        if self._sizing_limits is None:
            raise RuntimeError("automatic sizing is not configured")
        risk_limits = self._risk.limits
        if risk_limits is None:
            raise RuntimeError("risk limits are not configured")
        if signal.side not in (SignalSide.BUY, SignalSide.SELL) \
                or signal.stop_loss is None:
            raise ValueError("automatic sizing requires a directional signal with a stop")
        instrument = self.instruments.get(signal.instrument_id)
        if instrument is None:
            raise UnknownInstrument(signal.instrument_id)

        with self._submit_lock:
            now = self._clock()
            quote = self._quotes(signal.instrument_id)
            if quote is None:
                raise AutomaticSizingRejected("QUOTE_UNAVAILABLE")
            price, timestamp = quote
            age = now - timestamp
            max_age = risk_limits.max_market_data_age
            if age < timedelta(0) or age > max_age:
                raise AutomaticSizingRejected("STALE_OR_FUTURE_QUOTE")
            rate = self.portfolio.rate_to_base(instrument.currency)
            if rate <= 0:
                raise AutomaticSizingRejected("INVALID_FX_RATE")
            base_limits = self._sizing_limits
            local_limits = replace(
                base_limits,
                max_order_notional=base_limits.max_order_notional / rate,
                broker=replace(
                    base_limits.broker,
                    max_order_notional=(
                        base_limits.broker.max_order_notional / rate
                        if base_limits.broker.max_order_notional is not None else None),
                ),
            )
            position = self.portfolio.positions().get(instrument.instrument_id)
            sector = self._sector_of(instrument.instrument_id)
            reservations = self._open_order_reservations(
                instrument_id=instrument.instrument_id,
                currency=instrument.currency,
                sector=sector,
                reference_quote=quote,
                now=now,
            )
            if reservations is None:
                raise AutomaticSizingRejected("OPEN_ORDER_RESERVATION_UNAVAILABLE")
            reserved_total, reserved_sector, reserved_current, reserved_cash = reservations
            sizing = size_position(
                instrument,
                OrderSide(signal.side.value),
                entry_price=price,
                stop_price=signal.stop_loss,
                equity=self.portfolio.equity / rate,
                available_cash=max(
                    0.0,
                    self.portfolio.cash.get(instrument.currency.upper(), 0.0)
                    - reserved_cash / rate,
                ),
                limits=local_limits,
                gross_exposure=(self.portfolio.gross_exposure + reserved_total) / rate,
                sector_exposure=(
                    self.portfolio.sector_exposure().get(sector or "UNKNOWN", 0.0)
                    + reserved_sector.get(sector or "UNKNOWN", 0.0)
                ) / rate,
                current_position_notional=(
                    abs(position.quantity) * position.last_price if position else 0.0
                ) + reserved_current / rate,
            )
            if not sizing.approved:
                raise AutomaticSizingRejected(
                    sizing.reason or "NO_APPROVED_QUANTITY")
            sizing_audit = replace(
                audit or AuditContext(),
                sizing={
                    "method": "AUTOMATIC_SIZING",
                    "quantity": sizing.quantity,
                    "risk_budget": sizing.risk_budget,
                    "stop_distance": sizing.stop_distance,
                    "raw_quantity": sizing.raw_quantity,
                    "binding_constraint": sizing.binding_constraint,
                    "caps": dict(sizing.caps),
                    "sizing_quote": price,
                    "sizing_quote_at": timestamp.isoformat(),
                    "currency": instrument.currency,
                },
            )
            result = self.submit_signal(
                signal,
                sizing.quantity,
                actor=actor,
                audit=sizing_audit,
                strategy_decision=strategy_decision,
                client_order_id=client_order_id,
            )
            return result, sizing

    def submit_signal(
        self,
        signal: Signal,
        quantity: int,
        *,
        actor: str = "system",
        audit: AuditContext | None = None,
        strategy_decision: AggregatedDecision | None = None,
        client_order_id: str | None = None,
    ) -> TicketResult:
        """Route a priced deterministic signal through the normal risk/order boundary."""
        if self.mode is not TradingMode.PAPER:
            raise RuntimeError("signal submission is currently restricted to PAPER mode")
        if not isinstance(signal, Signal):
            raise TypeError("signal must be a Signal")
        if signal.side not in (SignalSide.BUY, SignalSide.SELL):
            raise ValueError("only BUY or SELL signals can be submitted")
        if signal.entry_price is None:
            raise ValueError("a priced signal is required")
        if audit is not None and not isinstance(audit, AuditContext):
            raise TypeError("audit must be an AuditContext or None")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        return self.submit(
            OrderTicket(
                instrument_id=signal.instrument_id,
                side=OrderSide(signal.side.value),
                quantity=quantity,
                order_type=OrderType.MARKET,
                strategy=signal.strategy,
                client_order_id=client_order_id,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                signal_id=signal.signal_id,
                signal=signal,
                market_regime=signal.regime,
                audit=audit,
            ),
            actor=actor,
            strategy_decision=strategy_decision,
        )

    @property
    def disabled_controls(self) -> dict[str, str]:
        return self._risk.disabled_controls

    def _halt_decision(self, ticket: OrderTicket, now: datetime) -> RiskDecision | None:
        reason = self._gate.blocked_reason() if self._gate is not None else None
        if reason is None and self.mode is TradingMode.LIVE and ticket.intent is not OrderIntent.EXIT:
            # Live entries need an active, approved strategy configuration; missing wiring counts as not approved.
            problem = (self._strategy_approval(ticket.strategy) if self._strategy_approval is not None
                       else "no strategy approval registry configured")
            if problem is not None:
                return RiskDecision(RiskDecisionStatus.REJECTED, now, f"STRATEGY_NOT_APPROVED: {problem}",
                                    signal_id=ticket.signal_id)
            info = self._provenance_source(
                ticket.strategy) if self._provenance_source is not None else None
            if info is None or ticket.parameters_hash != info.parameters_hash:
                return RiskDecision(RiskDecisionStatus.REJECTED, now,
                                    "PARAMETERS_UNVERIFIED: running parameters do not match the approved configuration",
                                    signal_id=ticket.signal_id)
        if reason is None and self.mode is TradingMode.LIVE:
            gaps = live_gaps(
                ticket.audit, is_exit=ticket.intent is OrderIntent.EXIT)
            if gaps:
                return RiskDecision(RiskDecisionStatus.REJECTED, now,
                                    f"AUDIT_CONTEXT_MISSING: live orders must include {', '.join(gaps)}",
                                    signal_id=ticket.signal_id)
        if reason is None or ticket.intent is OrderIntent.EXIT:
            return None  # exits stay possible while entries are halted
        return RiskDecision(RiskDecisionStatus.REJECTED, now, f"TRADING_HALTED: {reason}",
                            signal_id=ticket.signal_id)

    def _record_provenance(
        self, client_order_id: str, ticket: OrderTicket, now: datetime,
        market_data: tuple[float, datetime] | None,
    ) -> None:
        """Stored before the order is sent, so every decision can be traced to a strategy version and its data."""
        if self._store is None:
            return
        info = self._provenance_source(
            ticket.strategy) if self._provenance_source is not None else None
        self._store.execution_records.save(
            client_order_id=client_order_id, mode=self.mode.value, strategy_name=ticket.strategy,
            strategy_version=info.strategy_version if info else "UNVERSIONED",
            parameter_version=info.parameter_version if info else None,
            parameters_hash=ticket.parameters_hash or (
                info.parameters_hash if info else None),
            signal_id=ticket.signal_id, market_regime=ticket.market_regime,
            data_timestamp=market_data[1] if market_data else None,
            decision_timestamp=now, versioned=info is not None)

    def _record_audit(self, client_order_id: str, ticket: OrderTicket, now: datetime,
                      decision: RiskDecision, market_data: tuple[float, datetime] | None,
                      strategy_decision: AggregatedDecision | None) -> None:
        """Decision inputs are stored before the order is sent; the broker response is added afterwards."""
        if self._store is None:
            return
        if ticket.signal is not None:
            self._store.signals.save(ticket.signal)
        if strategy_decision is not None:
            self._store.strategy_decisions.save(strategy_decision)
        a = ticket.audit or AuditContext()
        self._store.order_audit.record(
            client_order_id,
            market_data={"instrument_id": ticket.instrument_id,
                         "price": market_data[0], "timestamp": market_data[1]} if market_data else None,
            technical_signals=dict(a.technical_signals) or None, news_signals=dict(a.news_signals) or None,
            ai_analysis_ids=list(a.ai_analysis_ids) or None, strategy_decision_id=a.strategy_decision_id,
            sizing=dict(a.sizing) or None, data_reference=a.data_reference, exit_reason=a.exit_reason,
            risk_inputs={"risk_decision_id": decision.decision_id, "status": decision.status,
                         "reason": decision.reason, "assessed_at": now, "stop_loss": ticket.stop_loss,
                         "take_profit": ticket.take_profit, "intent": ticket.intent})

    def _record_broker_response(self, order: ManagedOrder) -> None:
        if self._store is None or order.broker_order_id is None:
            return
        status = self.order_manager.broker_status(order.client_order_id)
        response = {k: getattr(status, k) for k in status.__slots__} if hasattr(status, "__slots__") else \
            {"broker_order_id": order.broker_order_id}
        self._store.order_audit.record(
            order.client_order_id, broker_response=response)

    def _persist_transition(self, order: ManagedOrder, event: Any) -> None:
        self._store.orders.save_with_events(
            order, self.order_manager.events(order.client_order_id))

    def cancel(self, client_order_id: str, *, actor: str = "system") -> ManagedOrder:
        self.order_manager.sync(client_order_id)
        order = self.order_manager.cancel(client_order_id)
        order = self.order_manager.sync(client_order_id)
        self._record_broker_response(order)
        if self._store is not None:
            self._store.orders.save_with_events(
                order, self.order_manager.events(client_order_id))
            self._store.audit.append(actor, "ORDER_CANCEL_REQUESTED", "order", client_order_id,
                                     {"mode": self.mode.value})
        return order

    # ---- internals ----
    def _context(
        self, t: OrderTicket, inst: Instrument, now: datetime,
        quote: tuple[float, datetime] | None,
    ) -> RiskContext:
        position = self.portfolio.positions().get(inst.instrument_id)
        orders = self.order_manager.orders()
        open_orders = self.order_manager.open_orders()
        stats = dict(self._stats(inst.instrument_id))
        stats.setdefault("orders_last_minute",
                         sum(1 for o in orders if now - o.timestamp <= timedelta(minutes=1)))
        stats.setdefault("recent_order_keys", frozenset(
            order_key_for(o.instrument_id, o.side.value, o.signal_id, o.quantity) for o in open_orders))
        today = now.date()
        reserved_buy_quantity = sum(
            order.remaining_quantity
            for order in open_orders
            if order.instrument_id == inst.instrument_id and order.side is OrderSide.BUY)
        reserved_sell_quantity = sum(
            order.remaining_quantity
            for order in open_orders
            if order.instrument_id == inst.instrument_id and order.side is OrderSide.SELL)
        reserved_new_positions = {
            order.instrument_id for order in open_orders
            if order.instrument_id not in self.portfolio.positions()
        }
        if position is not None:
            current_quantity = position.quantity
            current_side = position.side
            if current_side is PositionSide.LONG:
                current_quantity = max(0, current_quantity - reserved_sell_quantity)
            else:
                current_quantity = max(0, current_quantity - reserved_buy_quantity)
        elif reserved_buy_quantity and reserved_sell_quantity:
            current_quantity = reserved_buy_quantity + reserved_sell_quantity
            current_side = (
                PositionSide.SHORT if t.side is OrderSide.BUY else PositionSide.LONG)
        elif reserved_buy_quantity:
            current_quantity = reserved_buy_quantity
            current_side = PositionSide.LONG
        elif reserved_sell_quantity:
            current_quantity = reserved_sell_quantity
            current_side = PositionSide.SHORT
        else:
            current_quantity = 0
            current_side = None
        instrument_rate = self.portfolio.rate_to_base(inst.currency)
        portfolio_state = self.portfolio.risk_state(
            inst.instrument_id, sector=self._sector_of(inst.instrument_id), **stats)
        reservations = self._open_order_reservations(
            instrument_id=inst.instrument_id,
            currency=inst.currency,
            sector=self._sector_of(inst.instrument_id),
            reference_quote=quote,
            now=now,
        )
        if reservations is None:
            portfolio_state = replace(
                portfolio_state, gross_notional_exposure=float("inf"))
            reserved_current = 0.0
            reserved_cash = float("inf")
            reserved_sector = dict(portfolio_state.sector_exposure)
        else:
            reserved_total, reserved_sector_totals, reserved_current, reserved_cash = reservations
            reserved_sector = dict(portfolio_state.sector_exposure)
            for name, amount in reserved_sector_totals.items():
                reserved_sector[name] = reserved_sector.get(name, 0.0) + amount
        portfolio_state = replace(
            portfolio_state,
            gross_notional_exposure=(
                portfolio_state.gross_notional_exposure
                + (reserved_total if reservations is not None else 0.0)),
            sector_exposure=reserved_sector,
            current_position_notional=(
                portfolio_state.current_position_notional + reserved_current),
        )
        return RiskContext(
            assessed_at=now,
            market_data_timestamp=quote[1] if quote else None,
            market_data_valid=quote is not None,
            reference_price=quote[0] if quote else None,
            available_cash=max(
                0.0,
                self.portfolio.cash.get(inst.currency.upper(), 0.0)
                - reserved_cash / instrument_rate,
            ),
            estimated_fees=0.0,
            current_position_quantity=current_quantity,
            current_position_side=current_side,
            open_positions=len(self.portfolio.positions()) + len(reserved_new_positions),
            trades_today=sum(
                1 for f in self.portfolio.fills if f.timestamp.date() == today),
            intent=t.intent, stop_loss=t.stop_loss, take_profit=t.take_profit,
            signal=t.signal,
            portfolio=portfolio_state)

    def _open_order_reservations(
        self,
        *,
        instrument_id: str,
        currency: str,
        sector: str | None,
        reference_quote: tuple[float, datetime] | None,
        now: datetime,
    ) -> tuple[float, dict[str, float], float, float] | None:
        total = 0.0
        sector_totals: dict[str, float] = {}
        current = 0.0
        cash = 0.0
        risk_limits = self._risk.limits
        max_age = risk_limits.max_market_data_age if risk_limits is not None else None
        for order in self.order_manager.open_orders():
            order_instrument = self.instruments.get(order.instrument_id)
            if order_instrument is None:
                return None
            if order.limit_price is not None:
                price = order.limit_price
            else:
                quote = reference_quote if order.instrument_id == instrument_id \
                    else self._quotes(order.instrument_id)
                if quote is None:
                    return None
                age = now - quote[1]
                if age < timedelta(0) or (max_age is not None and age > max_age):
                    return None
                price = quote[0]
            rate = self.portfolio.rate_to_base(order_instrument.currency)
            notional = order.remaining_quantity * price * rate
            total += notional
            order_sector = self._sector_of(order.instrument_id) or "UNKNOWN"
            sector_totals[order_sector] = (
                sector_totals.get(order_sector, 0.0) + notional)
            if order.instrument_id == instrument_id:
                current += notional
            if order.side is OrderSide.BUY:
                cash += notional
        return total, sector_totals, current, cash

    def _persist(self, result: SubmissionResult, decision: RiskDecision, actor: str, action: str) -> None:
        if self._store is None or result.duplicate:
            return
        o = result.order
        self._store.risk_decisions.save(
            decision, {"client_order_id": o.client_order_id})
        self._store.orders.save_with_events(
            o, self.order_manager.events(o.client_order_id))
        self._store.audit.append(actor, action, "order", o.client_order_id, {
            "mode": self.mode.value, "status": o.status.value, "risk_decision_id": str(decision.decision_id),
            "risk_status": decision.status.value, "error": o.error})


def summarize_trades(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Headline statistics over stored trade rows (net_pnl in each trade's own currency)."""
    pnls = [float(r["net_pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "count": len(pnls),
        "net_pnl": sum(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "expectancy": sum(pnls) / len(pnls) if pnls else None,
    }
