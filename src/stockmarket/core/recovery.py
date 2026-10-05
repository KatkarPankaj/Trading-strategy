"""Restart recovery: restore persisted state, reconcile with the broker, and halt new entries on any doubt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .brokers import BrokerAdapter, BrokerError
from .models import Instrument, OrderSide, OrderStatus, OrderType
from .order_management import ManagedOrder, OrderEvent, OrderManager
from .portfolio import PortfolioManager
from .resilience import RetryPolicy, call_with_retry
from .trading_gate import TradingGate

GATE_CODE = "RECONCILIATION_FAILED"


class RecoveryError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Discrepancy:
    id: str  # stable, so an operator can acknowledge a specific finding
    kind: str
    subject: str
    local: str
    broker: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    checked_at: datetime
    discrepancies: tuple[Discrepancy, ...] = ()
    orders_restored: int = 0
    actions: tuple[str, ...] = field(default=())

    @property
    def clean(self) -> bool:
        return not self.discrepancies


# ---- loading persisted state ----
def order_from_row(row: Mapping[str, Any]) -> ManagedOrder:
    return ManagedOrder(
        client_order_id=row["client_order_id"], broker_order_id=row["broker_order_id"],
        instrument_id=row["instrument_id"], symbol=row["symbol"], side=OrderSide(
            row["side"]),
        quantity=row["quantity"], order_type=OrderType(row["order_type"]),
        limit_price=row["limit_price"], stop_price=row["stop_price"],
        timestamp=datetime.fromisoformat(row["timestamp"]), strategy=row["strategy"],
        signal_id=_uuid(row["signal_id"]), risk_decision_id=_uuid(row["risk_decision_id"]),
        status=OrderStatus(row["status"]), filled_quantity=row["filled_quantity"],
        average_fill_price=row["average_fill_price"], error=row["error"])


def _uuid(value: str | None) -> Any:
    from uuid import UUID
    return UUID(value) if value else None


def event_from_row(row: Mapping[str, Any]) -> OrderEvent:
    return OrderEvent(datetime.fromisoformat(row["timestamp"]),
                      OrderStatus(row["from_status"]
                                  ) if row["from_status"] else None,
                      OrderStatus(row["to_status"]), row["detail"],
                      row.get("fill_quantity"), row.get("fill_price"))


def rebuild_portfolio(
    store: Any,
    instruments: Mapping[str, Instrument],
    base_currency: str,
    starting_cash: float,
    fx_rates: Mapping[str, float] | None = None,
) -> PortfolioManager:
    """Replay persisted fills in time order onto a fresh portfolio; deterministic for a given history."""
    portfolio = PortfolioManager(base_currency, starting_cash)
    for ccy, rate in (fx_rates or {}).items():
        portfolio.set_fx_rate(ccy, rate)
    for row in store.fills.all():
        instrument = instruments.get(row["instrument_id"])
        if instrument is None:
            raise RecoveryError(
                f"fill references unknown instrument {row['instrument_id']}")
        portfolio.apply_fill(instrument, OrderSide(row["side"]), row["quantity"], row["price"],
                             datetime.fromisoformat(row["timestamp"]), fee=row["fee"],
                             slippage=row["slippage"])
    return portfolio


# ---- reconciliation ----
def reconcile_positions(portfolio: PortfolioManager, broker: BrokerAdapter) -> list[Discrepancy]:
    local = {p.instrument_id: p for p in portfolio.positions().values()}
    remote = {p.instrument_id: p for p in broker.positions()}
    out = []
    for iid in sorted(set(local) | set(remote)):
        a, b = local.get(iid), remote.get(iid)
        if a is None:
            out.append(Discrepancy(f"POSITION:{iid}:UNEXPECTED_AT_BROKER", "UNEXPECTED_POSITION", iid,
                                   "none", f"{b.side.value} {b.quantity}"))
        elif b is None:
            out.append(Discrepancy(f"POSITION:{iid}:MISSING_AT_BROKER", "MISSING_POSITION", iid,
                                   f"{a.side.value} {a.quantity}", "none"))
        elif (a.side, a.quantity) != (b.side, b.quantity):
            out.append(Discrepancy(f"POSITION:{iid}:QUANTITY_MISMATCH", "POSITION_MISMATCH", iid,
                                   f"{a.side.value} {a.quantity}", f"{b.side.value} {b.quantity}"))
    return out


def reconcile_orders(manager: OrderManager, broker: BrokerAdapter) -> tuple[list[Discrepancy], list[str]]:
    discrepancies: list[Discrepancy] = []
    actions: list[str] = []
    try:
        broker_open = {o.client_order_id: o for o in broker.open_orders()}
    except BrokerError as exc:
        return [Discrepancy("ORDERS:BROKER_QUERY_FAILED", "BROKER_ERROR", "open_orders", "", "", str(exc))], actions
    for order in manager.open_orders():
        cid = order.client_order_id
        if order.status in (OrderStatus.NEW, OrderStatus.VALIDATED):
            manager.mark_failed(
                cid, "interrupted before submission by restart")
            actions.append(f"{cid}: never sent, marked FAILED")
        elif order.broker_order_id is None:
            found = broker_open.get(cid)
            if found is None:
                discrepancies.append(Discrepancy(
                    f"ORDER:{cid}:STATE_UNKNOWN", "ORDER_STATE_UNKNOWN", cid, order.status.value,
                    "not found among broker open orders",
                    "may or may not have reached the broker; do not resend until verified"))
            else:
                manager.attach_broker_order(cid, found.broker_order_id)
                manager.sync(cid)
                actions.append(
                    f"{cid}: adopted broker id {found.broker_order_id}")
        else:
            try:
                manager.sync(cid)
            except BrokerError as exc:
                discrepancies.append(Discrepancy(
                    f"ORDER:{cid}:MISSING_AT_BROKER", "ORDER_MISSING", cid, order.status.value,
                    "unknown to broker", str(exc)))
    return discrepancies, actions


class RecoveryManager:
    def __init__(
        self,
        *,
        store: Any,
        order_manager: OrderManager,
        portfolio: PortfolioManager,
        broker: BrokerAdapter,
        gate: TradingGate,
        logger: Any = None,
        alerts: Any = None,
        retry: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store, self._orders, self._portfolio = store, order_manager, portfolio
        self._broker, self._gate, self._log, self._alerts = broker, gate, logger, alerts
        self._retry, self._sleep = retry, sleep

    def recover(self) -> ReconciliationReport:
        """Startup sequence: load state, connect, reconcile, and halt new entries unless clean."""
        restored = self._restore_orders()
        try:
            call_with_retry(self._broker.connect, self._retry,
                            retry_on=(ConnectionError, TimeoutError, BrokerError), sleep=self._sleep)
        except Exception as exc:
            report = ReconciliationReport(
                _now(), (Discrepancy("BROKER:UNREACHABLE",
                                     "BROKER_UNREACHABLE", "broker", "", "", str(exc)),),
                restored)
            self._conclude(report, "STARTUP_RECONCILIATION")
            return report
        report = self._reconcile(restored)
        self._conclude(report, "STARTUP_RECONCILIATION")
        return report

    def reconcile(self) -> ReconciliationReport:
        return self._reconcile(0)

    def resume(self, operator: str, note: str, acknowledged: Iterable[str] = ()) -> ReconciliationReport:
        """Explicit recovery: re-check, and clear the halt only if every remaining discrepancy is acknowledged."""
        if not operator.strip() or not note.strip():
            raise RecoveryError(
                "operator and note are required to resume trading")
        report = self._reconcile(0)
        acknowledged = set(acknowledged)
        outstanding = [
            d.id for d in report.discrepancies if d.id not in acknowledged]
        if outstanding:
            raise RecoveryError(
                f"unacknowledged discrepancies: {', '.join(outstanding)}")
        self._gate.clear(GATE_CODE)
        self._store.audit.append(operator, "RECOVERY_RESUMED", "system", "recovery", {
            "note": note, "acknowledged": sorted(acknowledged),
            "remaining": [d.id for d in report.discrepancies]})
        if self._log:
            self._log.warning("trading resumed after recovery",
                              operator=operator, note=note)
        return report

    # ---- internals ----
    def _restore_orders(self) -> int:
        count = 0
        for row in self._store.orders.all():
            order = order_from_row(row)
            events = tuple(event_from_row(e)
                           for e in self._store.orders.events(order.client_order_id))
            self._orders.restore(order, events)
            count += 1
        return count

    def _reconcile(self, restored: int) -> ReconciliationReport:
        try:
            order_issues, actions = reconcile_orders(
                self._orders, self._broker)
            position_issues = reconcile_positions(
                self._portfolio, self._broker)
        except BrokerError as exc:
            return ReconciliationReport(_now(), (Discrepancy(
                "BROKER:QUERY_FAILED", "BROKER_ERROR", "broker", "", "", str(exc)),), restored)
        return ReconciliationReport(_now(), tuple(order_issues + position_issues), restored, tuple(actions))

    def _conclude(self, report: ReconciliationReport, action: str) -> None:
        ids = [d.id for d in report.discrepancies]
        if report.clean:
            self._gate.clear(GATE_CODE)
        else:
            self._gate.halt(
                GATE_CODE, f"{len(ids)} discrepancy(ies): {', '.join(ids)[:300]}")
        self._store.audit.append("recovery", action, "system", "startup", {
            "clean": report.clean, "orders_restored": report.orders_restored,
            "discrepancies": [_asdict(d) for d in report.discrepancies],
            "actions": list(report.actions)})
        if self._log:
            (self._log.info if report.clean else self._log.error)(
                "startup reconciliation " +
                ("clean" if report.clean else "found discrepancies; new entries halted"),
                discrepancies=ids, actions=list(report.actions))
        if not report.clean and self._alerts is not None:
            from .observability import AlertType
            for d in report.discrepancies:
                kind = AlertType.UNEXPECTED_POSITION if d.kind.endswith("POSITION") or d.kind == "POSITION_MISMATCH" \
                    else AlertType.BROKER_DISCONNECTED if d.kind == "BROKER_UNREACHABLE" else AlertType.ORDER_REJECTED
                self._alerts.raise_alert(
                    kind, f"reconciliation: {d.kind} {d.subject}", key=d.id)


def _asdict(d: Discrepancy) -> dict[str, str]:
    return {"id": d.id, "kind": d.kind, "subject": d.subject, "local": d.local,
            "broker": d.broker, "detail": d.detail}


def _now() -> datetime:
    return datetime.now(timezone.utc)
