"""Order entry service: the single place where tickets become risk-checked, tracked orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import UUID

from .audit_trail import AuditContext, live_gaps
from .executors import TradingMode
from .models import Instrument, OrderSide, OrderType, RiskDecision, RiskDecisionStatus
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

QuoteSource = Callable[[str], "tuple[float, datetime] | None"]
MarketStats = Callable[[str], Mapping[str, Any]]


class UnknownInstrument(ValueError):
    pass


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
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if store is not None:  # write-ahead: each transition is stored before the next step runs
            order_manager.on_change = self._persist_transition

    def submit(self, ticket: OrderTicket, *, actor: str = "system") -> TicketResult:
        instrument = self.instruments.get(ticket.instrument_id)
        if instrument is None:
            raise UnknownInstrument(ticket.instrument_id)
        now = self._clock()
        client_order_id = ticket.client_order_id or new_client_order_id()

        decision = self._halt_decision(ticket, now) or self._risk.evaluate_proposal(
            instrument, side=ticket.side, quantity=ticket.quantity, order_type=ticket.order_type,
            created_at=now, context=self._context(ticket, instrument, now),
            limit_price=ticket.limit_price, stop_price=ticket.stop_price,
            strategy=ticket.strategy, signal_id=ticket.signal_id)
        request = OrderRequest(
            client_order_id=client_order_id, instrument_id=instrument.instrument_id,
            symbol=instrument.symbol, side=ticket.side, quantity=ticket.quantity,
            order_type=ticket.order_type, timestamp=now, strategy=ticket.strategy,
            signal_id=ticket.signal_id, limit_price=ticket.limit_price, stop_price=ticket.stop_price)
        self._record_provenance(client_order_id, ticket, now)
        self._record_audit(client_order_id, ticket, now, decision)
        result = self.order_manager.submit(request, decision)
        if not result.duplicate and not result.order.is_terminal:
            result = SubmissionResult(self.order_manager.sync(client_order_id))
        self._record_broker_response(result.order)
        self._persist(result, decision, actor, "ORDER_SUBMITTED")
        return TicketResult(result.order, decision, result.duplicate)

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

    def _record_provenance(self, client_order_id: str, ticket: OrderTicket, now: datetime) -> None:
        """Stored before the order is sent, so every decision can be traced to a strategy version and its data."""
        if self._store is None:
            return
        info = self._provenance_source(
            ticket.strategy) if self._provenance_source is not None else None
        quote = self._quotes(ticket.instrument_id)
        self._store.execution_records.save(
            client_order_id=client_order_id, mode=self.mode.value, strategy_name=ticket.strategy,
            strategy_version=info.strategy_version if info else "UNVERSIONED",
            parameter_version=info.parameter_version if info else None,
            parameters_hash=ticket.parameters_hash or (
                info.parameters_hash if info else None),
            signal_id=ticket.signal_id, market_regime=ticket.market_regime,
            data_timestamp=quote[1] if quote else None, decision_timestamp=now, versioned=info is not None)

    def _record_audit(self, client_order_id: str, ticket: OrderTicket, now: datetime,
                      decision: RiskDecision) -> None:
        """Decision inputs are stored before the order is sent; the broker response is added afterwards."""
        if self._store is None:
            return
        quote = self._quotes(ticket.instrument_id)
        a = ticket.audit or AuditContext()
        self._store.order_audit.record(
            client_order_id,
            market_data={"instrument_id": ticket.instrument_id,
                         "price": quote[0], "timestamp": quote[1]} if quote else None,
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
    def _context(self, t: OrderTicket, inst: Instrument, now: datetime) -> RiskContext:
        quote = self._quotes(inst.instrument_id)
        position = self.portfolio.positions().get(inst.instrument_id)
        orders = self.order_manager.orders()
        open_orders = [o for o in orders if not o.is_terminal]
        stats = dict(self._stats(inst.instrument_id))
        stats.setdefault("orders_last_minute",
                         sum(1 for o in orders if now - o.timestamp <= timedelta(minutes=1)))
        stats.setdefault("recent_order_keys", frozenset(
            order_key_for(o.instrument_id, o.side.value, o.signal_id, o.quantity) for o in open_orders))
        today = now.date()
        return RiskContext(
            assessed_at=now,
            market_data_timestamp=quote[1] if quote else None,
            market_data_valid=quote is not None,
            reference_price=quote[0] if quote else None,
            available_cash=self.portfolio.cash.get(inst.currency.upper(), 0.0),
            estimated_fees=0.0,
            current_position_quantity=position.quantity if position else 0,
            current_position_side=position.side if position else None,
            open_positions=len(self.portfolio.positions()),
            trades_today=sum(
                1 for f in self.portfolio.fills if f.timestamp.date() == today),
            intent=t.intent, stop_loss=t.stop_loss, take_profit=t.take_profit,
            portfolio=self.portfolio.risk_state(
                inst.instrument_id, sector=self._sector_of(inst.instrument_id), **stats))

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
