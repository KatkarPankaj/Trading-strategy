"""Emergency kill switch: stops new orders at once, can cancel open orders, never blocks risk-reducing exits."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .models import OrderSide, PositionSide

GATE_CODE = "KILL_SWITCH"
TRIGGERED, RESET = "KILL_SWITCH_TRIGGERED", "KILL_SWITCH_RESET"


class KillSwitchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    triggered_at: datetime
    code: str
    detail: str
    source: str  # "manual" or "automatic"
    actor: str
    already_active: bool
    cancelled: tuple[str, ...] = ()
    kept_reducing: tuple[str, ...] = ()
    cancel_errors: tuple[str, ...] = ()


class KillSwitch:
    def __init__(
        self,
        gate: Any,
        *,
        services: Callable[[], Iterable[Any]] = lambda: (),
        store: Any = None,
        logger: Any = None,
        alerts: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gate, self._services, self._store = gate, services, store
        self._log, self._alerts = logger, alerts
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.auto_check: Callable[[], list[tuple[str, str]]] | None = None

    @property
    def active(self) -> bool:
        return GATE_CODE in self._gate.reasons()

    def available(self) -> bool:
        """True when the switch could actually engage and be recorded."""
        return self._gate is not None and (self._store is None or bool(self._store.db.healthy()))

    def restore(self) -> bool:
        """After a restart, re-engage if the last recorded kill-switch action was a trigger."""
        if self._store is None:
            return False
        last = self._store.audit.latest((TRIGGERED, RESET))
        if last is not None and last["action"] == TRIGGERED:
            self._gate.halt(
                GATE_CODE, "restored after restart; reset required")
            return True
        return False

    def trigger(self, code: str, detail: str, *, source: str, actor: str,
                cancel_open_orders: bool = False) -> KillSwitchEvent:
        if source not in ("manual", "automatic") or not actor.strip():
            raise KillSwitchError(
                "source must be manual or automatic and actor is required")
        already = self.active
        # first, so no new order can slip through while we log
        self._gate.halt(GATE_CODE, f"{code}: {detail}")
        cancelled: list[str] = []
        kept: list[str] = []
        errors: list[str] = []
        if cancel_open_orders:
            self._cancel(actor, cancelled, kept, errors)
        event = KillSwitchEvent(self._clock(), code, detail, source, actor, already,
                                tuple(cancelled), tuple(kept), tuple(errors))
        self._record(TRIGGERED, event)
        return event

    def reset(self, operator: str, note: str, *, override: bool = False) -> None:
        """Explicit, audited re-enable. Automatic triggers must also be clear unless overridden."""
        if not operator.strip() or not note.strip():
            raise KillSwitchError(
                "operator and note are required to reset the kill switch")
        if not self.active:
            raise KillSwitchError("kill switch is not active")
        if self.auto_check is not None and not override:
            breaches = self.auto_check()
            if breaches:
                raise KillSwitchError(
                    "trigger conditions still present: " + "; ".join(c for c, _ in breaches))
        self._gate.clear(GATE_CODE)
        if self._store is not None:
            self._store.audit.append(operator, RESET, "system", "kill_switch", {
                                     "note": note, "override": override})
        if self._log:
            self._log.warning("kill switch reset",
                              operator=operator, note=note, override=override)

    # ---- internals ----
    def _cancel(self, actor: str, cancelled: list[str], kept: list[str], errors: list[str]) -> None:
        for service in self._services():
            for order in service.order_manager.open_orders():
                if self._reduces_position(service, order):
                    kept.append(order.client_order_id)
                    continue
                try:
                    service.cancel(order.client_order_id, actor=actor)
                    cancelled.append(order.client_order_id)
                except Exception as exc:  # keep going; every failure is reported, none swallowed
                    errors.append(
                        f"{order.client_order_id}: {type(exc).__name__}")

    @staticmethod
    def _reduces_position(service: Any, order: Any) -> bool:
        position = service.portfolio.positions().get(order.instrument_id)
        if position is None:
            return False
        opposite = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
        return order.side is opposite and order.quantity <= position.quantity

    def _record(self, action: str, event: KillSwitchEvent) -> None:
        payload = {"code": event.code, "detail": event.detail, "source": event.source,
                   "already_active": event.already_active, "cancelled": list(event.cancelled),
                   "kept_reducing": list(event.kept_reducing), "cancel_errors": list(event.cancel_errors)}
        if self._store is not None:
            self._store.audit.append(
                event.actor, action, "system", "kill_switch", payload)
        if self._log:
            self._log.critical(f"kill switch triggered: {event.code}", **{k: v for k, v in payload.items() if k != "detail"},
                               kill_detail=event.detail)
        if self._alerts is not None:
            from .observability import AlertType

            self._alerts.raise_alert(
                AlertType.KILL_SWITCH_TRIGGERED, f"{event.code}: {event.detail}", key=event.code)


@dataclass(frozen=True, slots=True)
class AutoTriggerPolicy:
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_consecutive_broker_errors: int = 3
    max_price_move: float = 0.10  # between consecutive observations of one instrument


class AutoTriggerMonitor:
    """Evaluates the automatic kill-switch conditions; call run_once() on a schedule or start() a background loop."""

    def __init__(
        self,
        kill_switch: KillSwitch,
        *,
        portfolio: Any,
        health: Any,
        policy: AutoTriggerPolicy,
        data_expected: Callable[[], bool] = lambda: True,
        unexpected_positions: Callable[[], Iterable[str]] = lambda: (),
        cancel_open_orders: bool = False,
    ) -> None:
        self._ks, self._pf, self._health, self._policy = kill_switch, portfolio, health, policy
        self._data_expected, self._unexpected = data_expected, unexpected_positions
        self._cancel = cancel_open_orders
        self._broker_errors = 0
        self._last_price: dict[str, float] = {}
        self._abnormal: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        kill_switch.auto_check = self.evaluate

    def record_broker_error(self) -> None:
        self._broker_errors += 1

    def record_broker_success(self) -> None:
        self._broker_errors = 0

    def record_price(self, instrument_id: str, price: float) -> None:
        last = self._last_price.get(instrument_id)
        self._last_price[instrument_id] = price
        if last and last > 0 and abs(price / last - 1) > self._policy.max_price_move:
            self._abnormal[
                instrument_id] = f"{instrument_id} moved {price / last - 1:+.1%} ({last} -> {price})"

    def clear_price_flags(self) -> None:
        self._abnormal.clear()

    def evaluate(self) -> list[tuple[str, str]]:
        p, pf = self._policy, self._pf
        out: list[tuple[str, str]] = []
        start_equity = pf.equity - pf.daily_pnl
        if start_equity > 0 and -pf.daily_pnl / start_equity >= p.max_daily_loss_pct:
            out.append(
                ("DAILY_LOSS_LIMIT", f"daily loss {-pf.daily_pnl / start_equity:.2%} >= {p.max_daily_loss_pct:.2%}"))
        if pf.drawdown >= p.max_drawdown_pct:
            out.append(
                ("MAX_DRAWDOWN", f"drawdown {pf.drawdown:.2%} >= {p.max_drawdown_pct:.2%}"))
        if self._broker_errors >= p.max_consecutive_broker_errors:
            out.append(("REPEATED_BROKER_ERRORS",
                       f"{self._broker_errors} consecutive broker errors"))
        if self._data_expected() and self._health.data_is_stale():
            out.append(
                ("STALE_MARKET_DATA", "market data is stale while markets are open"))
        out += [("ABNORMAL_PRICE_MOVE", d) for d in self._abnormal.values()]
        unexpected = sorted(self._unexpected())
        if unexpected:
            out.append(("UNEXPECTED_POSITION", ", ".join(unexpected)))
        if self._health.report()["status"] == "DOWN":
            out.append(("SYSTEM_HEALTH_FAILURE", "health report is DOWN"))
        return out

    def run_once(self) -> KillSwitchEvent | None:
        breaches = self.evaluate()
        if not breaches or self._ks.active:
            return None
        return self._ks.trigger(breaches[0][0], "; ".join(f"{c}: {d}" for c, d in breaches),
                                source="automatic", actor="auto-monitor", cancel_open_orders=self._cancel)

    def start(self, interval_seconds: float = 5.0) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.run_once()
                except Exception:  # monitoring must survive a bad evaluation; the failure is logged
                    import logging

                    logging.getLogger("stockmarket.kill_switch").exception(
                        "auto-trigger evaluation failed")

        self._thread = threading.Thread(
            target=loop, name="kill-switch-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
