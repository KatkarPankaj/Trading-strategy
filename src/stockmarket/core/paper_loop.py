"""Opt-in, in-process loop that repeats bounded PAPER research-to-order cycles.

The loop only schedules existing services: every entry still passes the deterministic
strategy, RiskEngine and OrderManager boundaries inside ``AutonomousPaperTradingService``.
It never runs in LIVE mode, never trades outside the market's regular session, stops
opening positions while the trading gate is halted (exits remain possible) and flattens
open positions shortly before the regular close.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .executors import TradingMode

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperLoopSettings:
    universe_id: str
    market: str
    interval_minutes: int = 15
    top_n: int = 3
    eod_flatten_minutes: int = 10
    max_consecutive_failures: int = 3
    operator: str = "paper-loop"
    monitor_interval_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.universe_id.strip() or not self.market.strip():
            raise ValueError("paper loop needs a universe_id and a market")
        for name, low, high in (("interval_minutes", 1, 240), ("top_n", 1, 100),
                                ("eod_flatten_minutes", 1, 120),
                                ("max_consecutive_failures", 1, 100),
                                ("monitor_interval_seconds", 1, 300)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if not self.operator.strip():
            raise ValueError("operator must be non-empty")


class PaperTradingLoop:
    def __init__(
        self,
        *,
        cycles: Any,
        positions: Any,
        gate: Any,
        markets: Any,
        settings: PaperLoopSettings,
        clock: Callable[[], datetime] | None = None,
        reconcile_evidence: Callable[[], Any] | None = None,
        entries_enabled: bool = True,
        record_control: Callable[[bool, str, str], None] | None = None,
    ) -> None:
        if positions.trading.mode is not TradingMode.PAPER:
            raise ValueError("the paper loop requires PAPER execution")
        markets.get(settings.market)  # unknown markets fail at construction
        self.cycles = cycles
        self.positions = positions
        self.gate = gate
        self.markets = markets
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconcile_evidence = reconcile_evidence
        self.record_control = record_control
        self._entries_enabled = entries_enabled
        self._last_scan_key: str | None = None
        self._last_scan_run: str | None = None
        self._last_evidence: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._state = "IDLE"
        self._last: dict[str, Any] | None = None
        self._ticks = 0
        self._recovered: list[str] = []
        self._recovery_failures: list[dict[str, str]] = []
        self.wait_poll_seconds = 30.0

    # --- deterministic scheduling primitives -------------------------------------------
    def bucket(self, at: datetime) -> datetime:
        self._aware(at)
        at = at.astimezone(timezone.utc)
        minutes = at.hour * 60 + at.minute
        floored = minutes - minutes % self.settings.interval_minutes
        return at.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)

    def idempotency_key(self, bucket: datetime) -> str:
        return f"loop-{self.settings.universe_id}-{bucket.strftime('%Y%m%dT%H%M')}"

    def _close_at(self, now: datetime) -> datetime:
        market = self.markets.get(self.settings.market)
        zone = ZoneInfo(market.timezone)
        day = now.astimezone(zone).date()
        return datetime.combine(day, market.calendar.close_time_on(day), tzinfo=zone)

    def recover_unfinished(self) -> list[str]:
        """Resume runs interrupted by a restart; a failing recovery is reported, not hidden."""
        with self._lock:
            return self._recover_unfinished()

    def _recover_unfinished(self) -> list[str]:
        recovered: list[str] = []
        if not self._entries_enabled:
            if self.reconcile_evidence is not None:
                self._last_evidence = self.reconcile_evidence()
            return recovered
        for run in self.cycles.repository.unfinished():
            try:
                result = self.cycles.recover(run["run_id"])
                if result is not None and result.get("status") != "COMPLETE" \
                        and result.get("stage") != "RECOVERY_OBSOLETE":
                    raise RuntimeError(f"recovery ended with {result.get('status')}")
                recovered.append(run["run_id"])
            except Exception as exc:  # noqa: BLE001 - reported and counted, the loop keeps its fail-closed limits
                _log.exception("paper loop could not recover cycle %s", run["run_id"])
                self._recovery_failures.append({
                    "run_id": run["run_id"], "error": f"{type(exc).__name__}: {exc}"})
                self._failures += 1
                if self._failures >= self.settings.max_consecutive_failures:
                    self._state = "STOPPED_AFTER_FAILURES"
                    break
        self._recovered = recovered
        if self.reconcile_evidence is not None:
            self._last_evidence = self.reconcile_evidence()
        return recovered

    def tick(self) -> dict[str, Any]:
        """Run at most one cycle or one exit pass for the current time."""
        with self._lock:
            if self._stop.is_set():
                return {"action": "STOPPED", "consecutive_failures": self._failures}
            now = self.clock()
            self._ticks += 1
            market = self.settings.market
            try:
                self._aware(now)
                if not self.markets.is_regular_session(market, now):
                    result = {"action": "MARKET_CLOSED"}
                elif now >= self._close_at(now) - timedelta(minutes=self.settings.eod_flatten_minutes):
                    result = {"action": "END_OF_DAY_FLATTEN",
                              "result": self.positions.flatten(market=market)}
                    self._check_positions(result["result"])
                elif self.gate.halted or not self._entries_enabled \
                        or self._state == "STOPPED_AFTER_FAILURES":
                    # entries are blocked; keep stop/target exits running
                    result = {"action": "EXITS_ONLY",
                              "reason": self.gate.blocked_reason() or (
                                  "STOPPED_AFTER_FAILURES" if self._state == "STOPPED_AFTER_FAILURES"
                                  else "OPERATOR_PAUSED_ENTRIES"),
                              "result": self.positions.manage()}
                    self._check_positions(result["result"])
                else:
                    bucket = self.bucket(now)
                    key = self.idempotency_key(bucket)
                    if key == self._last_scan_key:
                        result = {"action": "MONITOR", "run_id": self._last_scan_run,
                                  "result": self.positions.manage()}
                        self._check_positions(result["result"])
                    else:
                        self._check_positions(self.positions.manage())
                        # Collection integrity is checked before admitting another automatic scan.
                        if self.reconcile_evidence is not None:
                            self._last_evidence = self.reconcile_evidence()
                        self._last_scan_key = key
                        run = self.cycles.run(
                            self.settings.universe_id, idempotency_key=key,
                            operator=self.settings.operator,
                            top_n=self.settings.top_n)
                        self._last_scan_run = run.get("run_id")
                        result = {"action": "CYCLE", "idempotency_key": key,
                                  "run_id": run.get("run_id"), "status": run.get("status")}
                        if run.get("status") != "COMPLETE":
                            raise RuntimeError(
                                f"paper cycle {run.get('run_id')} ended with {run.get('status')}")
                if self.reconcile_evidence is not None:
                    self._last_evidence = self.reconcile_evidence()
                if result["action"] == "CYCLE" and self._state != "STOPPED_AFTER_FAILURES":
                    self._failures = 0
            except Exception as exc:  # noqa: BLE001 - counted; repeated failures stop the loop
                self._failures += 1
                _log.exception("paper loop tick failed")
                result = {"action": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
                if self._failures >= self.settings.max_consecutive_failures:
                    self._state = "STOPPED_AFTER_FAILURES"
            result["at"] = now.isoformat()
            result["consecutive_failures"] = self._failures
            self._last = result
            return result

    @staticmethod
    def _check_positions(result: dict[str, Any]) -> None:
        failed = [row for row in (*result.get("orders", []), *result.get("positions", []))
                  if row.get("status") in {"MONITOR_FAILED", "RECONCILE_FAILED",
                                          "FLATTEN_FAILED", "REJECTED", "FAILED"}]
        if failed:
            raise RuntimeError(f"paper position monitoring failed: {failed}")

    def set_entries_enabled(self, enabled: bool, *, operator: str, reason: str) -> dict[str, Any]:
        if not isinstance(enabled, bool) or not operator.strip() or not reason.strip():
            raise ValueError("entry control requires a boolean, operator and reason")
        with self._lock:
            if enabled and self._state == "STOPPED_AFTER_FAILURES":
                raise ValueError("resolve failures and restart the API before resuming automatic entries")
            if enabled != self._entries_enabled:
                if self.record_control is not None:
                    self.record_control(enabled, operator, reason)
                self._entries_enabled = enabled
            return self.status()

    # --- background thread ------------------------------------------------------------
    def seconds_until_next(self, now: datetime) -> float:
        nxt = self.bucket(now) + timedelta(minutes=self.settings.interval_minutes)
        return max(1.0, (nxt - now).total_seconds())

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._state == "STOPPED_AFTER_FAILURES":
            raise RuntimeError("restart the API after resolving paper-loop failures")
        self._stop.clear()
        self._state = "RUNNING"
        self._thread = threading.Thread(target=self._run, name="paper-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self._state = "STOPPING"
                raise RuntimeError("paper loop is still processing; providers must remain open")
        if self._state == "RUNNING":
            self._state = "STOPPED"

    def _run(self) -> None:
        try:
            self.recover_unfinished()
        except Exception:
            _log.exception("paper loop startup evidence reconciliation failed")
            self._state = "STOPPED_AFTER_FAILURES"
            self._failures += 1
        while not self._stop.is_set():
            self.tick()
            if self._stop.is_set():
                break
            self._stop.wait(min(self.wait_poll_seconds,
                                self.settings.monitor_interval_seconds,
                                self.seconds_until_next(self.clock())))

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "trading_mode": TradingMode.PAPER.value,
            "universe_id": self.settings.universe_id,
            "market": self.settings.market,
            "interval_minutes": self.settings.interval_minutes,
            "top_n": self.settings.top_n,
            "eod_flatten_minutes": self.settings.eod_flatten_minutes,
            "monitor_interval_seconds": self.settings.monitor_interval_seconds,
            "entries_enabled": self._entries_enabled and not self._stop.is_set()
                               and self._state != "STOPPED_AFTER_FAILURES",
            "operator_entries_enabled": self._entries_enabled,
            "evidence_collection": self._last_evidence,
            "consecutive_failures": self._failures,
            "ticks": self._ticks,
            "recovered_runs": list(self._recovered),
            "last": self._last,
            "recovery_failures": list(self._recovery_failures),
        }

    @staticmethod
    def _aware(at: datetime) -> None:
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("paper-loop clock must be timezone-aware")
