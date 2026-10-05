"""Health monitoring: data, broker, database, last market data, last order result, error counts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

from .structured_logging import ErrorCounter

OK, DEGRADED, DOWN = "OK", "DEGRADED", "DOWN"


@dataclass(frozen=True, slots=True)
class CheckResult:
    healthy: bool
    detail: str = ""


class HealthMonitor:
    """Aggregates registered checks plus recorded activity into one JSON-ready report."""

    def __init__(
        self,
        *,
        max_market_data_age: timedelta = timedelta(minutes=5),
        errors: ErrorCounter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_market_data_age <= timedelta(0):
            raise ValueError("max_market_data_age must be positive")
        self._max_age = max_market_data_age
        self.errors = errors or ErrorCounter()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._checks: dict[str, Callable[[], CheckResult | bool]] = {}
        self._last_market_data: datetime | None = None
        self._last_order: tuple[datetime, str] | None = None
        self._lock = RLock()

    def register_check(self, name: str, check: Callable[[], CheckResult | bool]) -> None:
        """e.g. 'database' -> db.healthy, 'broker' -> lambda: broker.is_connected."""
        self._checks[name] = check

    def record_market_data(self, timestamp: datetime) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("market data timestamp must be timezone-aware")
        with self._lock:
            if self._last_market_data is None or timestamp > self._last_market_data:
                self._last_market_data = timestamp

    def record_order_result(self, status: str) -> None:
        """Call with the final status of an order; only successful outcomes update the 'last success'."""
        if status in ("FILLED", "PARTIALLY_FILLED", "ACCEPTED", "CANCELLED"):
            with self._lock:
                self._last_order = (self._clock(), status)

    def data_is_stale(self) -> bool:
        with self._lock:
            last = self._last_market_data
        return last is None or self._clock() - last > self._max_age

    def report(self) -> dict[str, Any]:
        now = self._clock()
        checks: dict[str, dict[str, Any]] = {}
        for name, fn in self._checks.items():
            try:
                raw = fn()
                result = raw if isinstance(
                    raw, CheckResult) else CheckResult(bool(raw))
            except Exception as exc:  # a failing check is itself an unhealthy result
                result = CheckResult(
                    False, f"check raised {type(exc).__name__}: {exc}")
            checks[name] = {"healthy": result.healthy, "detail": result.detail}
        with self._lock:
            last_data, last_order = self._last_market_data, self._last_order
        stale = last_data is None or now - last_data > self._max_age
        checks["market_data"] = {
            "healthy": not stale,
            "detail": "no market data received" if last_data is None
            else f"last update {(now - last_data).total_seconds():.0f}s ago",
        }
        unhealthy = [n for n, c in checks.items() if not c["healthy"]]
        if not unhealthy:
            status = OK
        elif any(n in ("database", "broker") for n in unhealthy):
            status = DOWN
        else:
            status = DEGRADED
        return {
            "status": status,
            "timestamp": now.isoformat(),
            "checks": checks,
            "last_market_data_timestamp": last_data.isoformat() if last_data else None,
            "market_data_age_seconds": (now - last_data).total_seconds() if last_data else None,
            "last_successful_order": (
                {"timestamp": last_order[0].isoformat(), "status": last_order[1]} if last_order else None),
            "error_counts": self.errors.snapshot(),
            "total_errors": self.errors.total(),
        }
