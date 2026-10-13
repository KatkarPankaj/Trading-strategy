"""Independent single-flight PAPER monitoring; deadlines detect, never cancel execution."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .executors import TradingMode

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProtectiveMonitorSettings:
    interval_seconds: float = 30
    latency_budget_seconds: float = 10
    lock_timeout_seconds: float = 1
    max_items: int = 100
    quote_timeout_seconds: float = 2

    def __post_init__(self) -> None:
        for name in ("interval_seconds", "latency_budget_seconds", "lock_timeout_seconds",
                     "quote_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) \
                    or not math.isfinite(value) or value <= 0 or value > 300:
                raise ValueError(f"{name} must be finite and in (0, 300]")
        if self.lock_timeout_seconds > self.latency_budget_seconds:
            raise ValueError("lock timeout must not exceed monitoring latency budget")
        if self.quote_timeout_seconds > self.latency_budget_seconds:
            raise ValueError("quote timeout must not exceed monitoring latency budget")
        if isinstance(self.max_items, bool) or not isinstance(self.max_items, int) \
                or not 1 <= self.max_items <= 10000:
            raise ValueError("max_items must be an integer in [1, 10000]")


class ProtectiveMonitor:
    def __init__(
        self, *, positions: Any, markets: Any, market: str, flatten_minutes: int,
        settings: ProtectiveMonitorSettings, clock: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if positions.trading.mode is not TradingMode.PAPER:
            raise ValueError("protective monitor requires PAPER mode")
        markets.get(market)
        self.positions = positions
        self.markets = markets
        self.market = market
        self.flatten_minutes = flatten_minutes
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._flight = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self.enabled = False
        self.recovery_complete = False
        self._state = "DISABLED"
        self._started: datetime | None = None
        self._ended: datetime | None = None
        self._active_since: float | None = None
        self._success: datetime | None = None
        self._success_mono: float | None = None
        self._duration: float | None = None
        self._maximum = 0.0
        self._recent: deque[float] = deque(maxlen=20)
        self._misses = 0
        self._miss_recorded = False
        self._failures = 0
        self._cycles = 0
        self._error: str | None = None
        self._result: dict[str, Any] | None = None

    def enable(self) -> None:
        with self._lock:
            if self._active_since is not None:
                raise RuntimeError("cannot re-enable an active protective cycle")
            self.enabled = True
            self._state = "RECOVERING"
            self.recovery_complete = False
            self._success = None
            self._success_mono = None
            self._error = None
            self._stop.clear()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.enable()
        self._thread = threading.Thread(target=self._run, name="paper-protective-monitor", daemon=True)
        self._watchdog = threading.Thread(
            target=self._watch, name="paper-protective-watchdog", daemon=True)
        self._thread.start()
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stop.wait(min(1., self.settings.latency_budget_seconds)):
            self.status()

    def _run(self) -> None:
        while not self._stop.is_set():
            due = self.monotonic() + self.settings.interval_seconds
            self.run_once()
            self._stop.wait(max(0., due - self.monotonic()))

    def run_once(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "DISABLED"}
        if not self._flight.acquire(blocking=False):
            return {"status": "BUSY", "error": "monitoring cycle already in progress"}
        try:
            if self._stop.is_set():
                return {"status": "STOPPED"}
            started = self.monotonic()
            with self._lock:
                self._started = self.clock()
                self._active_since = started
                self._miss_recorded = False
                self._state = "MONITORING"
            _log.info("protective monitoring started", extra={
                "component": "paper-protective-monitor", "started_at": self._started})
            result: dict[str, Any] = {}
            error = None
            try:
                now = self.clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("protective clock must be timezone-aware")
                options = dict(
                    max_items=self.settings.max_items,
                    deadline=started + self.settings.latency_budget_seconds,
                    monotonic=self.monotonic, lock_timeout=self.settings.lock_timeout_seconds)
                if not self.markets.is_regular_session(self.market, now):
                    # Sync persisted pending state without fetching out-of-session quotes.
                    result = self.positions.manage(**options)
                else:
                    market = self.markets.get(self.market)
                    day = now.astimezone(ZoneInfo(market.timezone)).date()
                    close = datetime.combine(
                        day, market.calendar.close_time_on(day), tzinfo=ZoneInfo(market.timezone))
                    if now >= close - timedelta(minutes=self.flatten_minutes):
                        result = self.positions.flatten(market=self.market, **options)
                    else:
                        result = self.positions.manage(**options)
                failures = [row for row in (*result.get("orders", []), *result.get("positions", []))
                            if row.get("status") in {
                                "MONITOR_FAILED", "RECONCILE_FAILED", "FLATTEN_FAILED",
                                "REJECTED", "FAILED", "ENTRY_ORDER_PENDING"}]
                if failures or result.get("incomplete"):
                    raise RuntimeError("protective reconciliation incomplete or failed")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _log.exception("protective monitoring failed")
            ended = self.monotonic()
            duration = max(0., ended - started)
            with self._lock:
                self._ended = self.clock()
                self._duration = duration
                self._maximum = max(self._maximum, duration)
                self._recent.append(duration)
                self._cycles += 1
                if duration > self.settings.latency_budget_seconds:
                    if not self._miss_recorded:
                        self._misses += 1
                    error = error or "MONITORING_LATENCY_BUDGET_EXCEEDED"
                self._active_since = None
                self._result = result
                self._error = error
                self._state = "FAILED" if error else "HEALTHY"
                if error:
                    self._failures += 1
                else:
                    self._failures = 0
                    self._success = self._ended
                    self._success_mono = ended
            _log.info("protective monitoring completed", extra={
                "component": "paper-protective-monitor", "duration_seconds": duration,
                "monitor_status": "FAILED" if error else "HEALTHY",
                "positions_checked": len(result.get("positions", [])),
                "pending_orders_checked": len(result.get("orders", [])),
                "ended_at": self._ended})
            return {"status": "FAILED" if error else "HEALTHY", "result": result, "error": error}
        finally:
            self._flight.release()

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self.monotonic()
            active_age = None if self._active_since is None else max(0., now - self._active_since)
            stalled = active_age is not None and active_age > self.settings.latency_budget_seconds
            if stalled and not self._miss_recorded:
                self._misses += 1
                self._miss_recorded = True
                _log.error("protective monitoring deadline missed", extra={
                    "component": "paper-protective-monitor", "active_seconds": active_age})
            age = None if self._success_mono is None else max(0., now - self._success_mono)
            stale = age is None or age > self.settings.interval_seconds + self.settings.latency_budget_seconds
            result = self._result or {}
            return {
                "state": "STALLED" if stalled else (
                    "STALE" if self.enabled and stale and self._state == "HEALTHY" else self._state),
                "trading_mode": "PAPER", "enabled": self.enabled,
                "recovery_complete": self.recovery_complete,
                "entry_ready": self.enabled and self.recovery_complete and not self._stop.is_set()
                               and not stalled and not stale and self._error is None,
                "last_started_at": self._started, "last_ended_at": self._ended,
                "last_successful_at": self._success, "success_age_seconds": age,
                "last_duration_seconds": self._duration,
                "maximum_duration_seconds": max(self._maximum, active_age or 0.),
                "recent_durations_seconds": list(self._recent), "active_seconds": active_age,
                "deadline_misses": self._misses, "consecutive_failures": self._failures,
                "cycles": self._cycles, "last_error": "MONITORING_STALLED" if stalled else (
                    "MONITORING_STALE" if stale and self._success is not None else self._error),
                "positions_checked": len(result.get("positions", [])),
                "pending_orders_checked": len(result.get("orders", [])),
                "interval_seconds": self.settings.interval_seconds,
                "latency_budget_seconds": self.settings.latency_budget_seconds,
                "lock_timeout_seconds": self.settings.lock_timeout_seconds,
                "max_items": self.settings.max_items,
                "quote_timeout_seconds": self.settings.quote_timeout_seconds,
                "hard_real_time": False,
            }

    def entry_blocker(self) -> str | None:
        if not self.enabled:
            return None
        return None if self.status()["entry_ready"] else "PROTECTIVE_MONITOR_NOT_READY"

    def scan_check(self) -> dict[str, Any]:
        if not self.enabled:
            return self.positions.manage()
        if self.entry_blocker():
            return {"incomplete": True, "positions": [
                {"status": "MONITOR_FAILED", "error": "PROTECTIVE_MONITOR_NOT_READY"}]}
        # Supervisor owns polling while enabled; no provider calls on the scan worker.
        return {"orders": [], "positions": [], "supervisor": self.status()}

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self, timeout: float = 30) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("shutdown timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        self.request_stop()
        if self._thread is not None:
            self._thread.join(max(0., deadline - time.monotonic()))
        if self._watchdog is not None:
            self._watchdog.join(max(0., deadline - time.monotonic()))
        if (self._thread is not None and self._thread.is_alive()) or self._flight.locked() \
                or (self._watchdog is not None and self._watchdog.is_alive()):
            with self._lock:
                self._state = "STOPPING"
            raise RuntimeError("protective monitoring still active; providers must remain open")
        with self._lock:
            self._state = "STOPPED"
