"""Alerting: de-duplicated alerts for the operational conditions that need a human or a kill switch."""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from .structured_logging import StructuredLogger


class AlertType(str, Enum):
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    DATA_STALE = "DATA_STALE"
    REPEATED_ORDER_FAILURES = "REPEATED_ORDER_FAILURES"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
    ORDER_REJECTED = "ORDER_REJECTED"
    STRATEGY_EXCEPTION = "STRATEGY_EXCEPTION"
    SYSTEM_CRASH = "SYSTEM_CRASH"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"


_SEVERITY = {
    AlertType.BROKER_DISCONNECTED: "CRITICAL",
    AlertType.DATA_STALE: "ERROR",
    AlertType.REPEATED_ORDER_FAILURES: "CRITICAL",
    AlertType.DAILY_LOSS_LIMIT: "CRITICAL",
    AlertType.DRAWDOWN_LIMIT: "CRITICAL",
    AlertType.UNEXPECTED_POSITION: "CRITICAL",
    AlertType.ORDER_REJECTED: "WARNING",
    AlertType.STRATEGY_EXCEPTION: "ERROR",
    AlertType.SYSTEM_CRASH: "CRITICAL",
    AlertType.KILL_SWITCH_TRIGGERED: "CRITICAL",
}


@dataclass(frozen=True, slots=True)
class Alert:
    type: AlertType
    severity: str
    message: str
    timestamp: datetime
    key: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)


AlertChannel = Callable[[Alert], None]


class AlertManager:
    """Raises each (type, key) at most once per cooldown; channels are e.g. e-mail, chat, a kill switch."""

    def __init__(
        self,
        logger: StructuredLogger,
        channels: Iterable[AlertChannel] = (),
        *,
        cooldown: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._log = logger
        self._channels = list(channels)
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last: dict[tuple[AlertType, str], datetime] = {}
        self._history: list[Alert] = []
        self._lock = RLock()

    def add_channel(self, channel: AlertChannel) -> None:
        self._channels.append(channel)

    @property
    def history(self) -> tuple[Alert, ...]:
        with self._lock:
            return tuple(self._history)

    def raise_alert(self, type_: AlertType, message: str, *, key: str = "",
                    **context: Any) -> Alert | None:
        """Returns None when suppressed by the cooldown."""
        now = self._clock()
        with self._lock:
            last = self._last.get((type_, key))
            if last is not None and now - last < self._cooldown:
                return None
            self._last[(type_, key)] = now
            alert = Alert(type_, _SEVERITY[type_],
                          message, now, key, dict(context))
            self._history.append(alert)
        self._log.log(
            alert.severity, f"ALERT {type_.value}: {message}", alert_type=type_.value, **context)
        for channel in self._channels:
            try:
                channel(alert)
            except Exception:
                self._log.error("alert channel failed",
                                alert_type=type_.value, channel=repr(channel))
        return alert


class AlertEvaluator:
    """Turns raw observations into alerts; wire its methods to the components that observe them."""

    def __init__(self, alerts: AlertManager, *, max_data_age: timedelta = timedelta(minutes=5),
                 max_consecutive_order_failures: int = 3,
                 clock: Callable[[], datetime] | None = None) -> None:
        if max_consecutive_order_failures < 1:
            raise ValueError(
                "max_consecutive_order_failures must be at least 1")
        self._alerts = alerts
        self._max_age = max_data_age
        self._max_failures = max_consecutive_order_failures
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures = 0

    def check_broker(self, connected: bool, broker: str = "broker") -> Alert | None:
        if connected:
            return None
        return self._alerts.raise_alert(AlertType.BROKER_DISCONNECTED,
                                        f"{broker} is disconnected", key=broker)

    def check_data(self, last_market_data: datetime | None, source: str = "market_data") -> Alert | None:
        now = self._clock()
        if last_market_data is None or now - last_market_data > self._max_age:
            age = "never received" if last_market_data is None else f"{(now - last_market_data).total_seconds():.0f}s old"
            return self._alerts.raise_alert(AlertType.DATA_STALE, f"{source} is stale ({age})", key=source)
        return None

    def on_order_result(self, status: str, client_order_id: str, reason: str | None = None) -> Alert | None:
        """REJECTED is reported individually; consecutive FAILED orders trigger the repeated-failure alert."""
        if status == "REJECTED":
            return self._alerts.raise_alert(
                AlertType.ORDER_REJECTED, f"order {client_order_id} rejected: {reason or 'no reason'}",
                key=client_order_id, order_id=client_order_id)
        if status == "FAILED":
            self._failures += 1
            if self._failures >= self._max_failures:
                return self._alerts.raise_alert(
                    AlertType.REPEATED_ORDER_FAILURES,
                    f"{self._failures} consecutive order failures", key="orders", order_id=client_order_id)
        elif status in ("FILLED", "ACCEPTED", "PARTIALLY_FILLED"):
            self._failures = 0
        return None

    def check_portfolio(self, *, daily_pnl_fraction: float, drawdown: float,
                        max_daily_loss: float, max_drawdown: float) -> list[Alert]:
        """Fractions of equity; losses are negative daily_pnl_fraction."""
        out = []
        if -daily_pnl_fraction >= max_daily_loss:
            a = self._alerts.raise_alert(AlertType.DAILY_LOSS_LIMIT,
                                         f"daily loss {-daily_pnl_fraction:.2%} reached limit {max_daily_loss:.2%}",
                                         key="daily_loss")
            out.append(a)
        if drawdown >= max_drawdown:
            a = self._alerts.raise_alert(AlertType.DRAWDOWN_LIMIT,
                                         f"drawdown {drawdown:.2%} reached limit {max_drawdown:.2%}",
                                         key="drawdown")
            out.append(a)
        return [a for a in out if a is not None]

    def check_positions(self, actual: Iterable[str], expected: Iterable[str]) -> list[Alert]:
        """Positions held that no known order or signal explains."""
        unexpected = sorted(set(actual) - set(expected))
        alerts = [self._alerts.raise_alert(AlertType.UNEXPECTED_POSITION,
                                           f"unexpected position in {iid}", key=iid, instrument_id=iid)
                  for iid in unexpected]
        return [a for a in alerts if a is not None]

    def on_strategy_exception(self, strategy: str, exc: BaseException) -> Alert | None:
        return self._alerts.raise_alert(
            AlertType.STRATEGY_EXCEPTION, f"strategy {strategy} raised {type(exc).__name__}: {exc}",
            key=f"{strategy}:{type(exc).__name__}", strategy=strategy)

    def install_crash_handler(self) -> None:
        """Route uncaught exceptions to a SYSTEM_CRASH alert before the default handler runs."""
        previous = sys.excepthook

        def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
            if not issubclass(exc_type, KeyboardInterrupt):
                self._alerts.raise_alert(
                    AlertType.SYSTEM_CRASH, f"unhandled {exc_type.__name__}: {exc}", key="crash",
                    traceback="".join(traceback.format_exception(exc_type, exc, tb))[-2000:])
            previous(exc_type, exc, tb)
        sys.excepthook = hook
