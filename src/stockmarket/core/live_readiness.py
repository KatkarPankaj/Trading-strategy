"""Live-trading readiness: every checklist item is computed from real system state, never assumed."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .executors import (
    LIVE_CONFIRMATION_PHRASE,
    ExecutionSettings,
    LiveBrokerClient,
    LiveExecutor,
    LiveReadiness,
    LiveTradingRefused,
    TradingMode,
)

_NTP_EPOCH_OFFSET = 2208988800
_RESEARCH_ONLY = {"yahoo", "mock"}


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    max_clock_offset_seconds: float = 1.0
    min_paper_filled_orders: int = 20
    min_paper_days: int = 5


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checked_at: datetime
    checks: tuple[CheckOutcome, ...]

    @property
    def ready(self) -> bool:
        return all(c.passed for c in self.checks)

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def to_live_readiness(self) -> LiveReadiness:
        return LiveReadiness(**{c.name: c.passed for c in self.checks})


def sntp_offset(server: str = "pool.ntp.org", timeout: float = 3.0) -> float:
    """Seconds the local clock is ahead of the NTP server (negative means behind)."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sent = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        received = time.time()
    seconds, fraction = struct.unpack("!II", data[40:48])
    server_time = seconds - _NTP_EPOCH_OFFSET + fraction / 2**32
    return (sent + received) / 2 - server_time


class LiveReadinessChecker:
    def __init__(
        self,
        *,
        settings: Any,
        broker: Any,
        store: Any,
        health: Any,
        recovery: Any,
        gate: Any,
        risk_engine: Any,
        risk_limits: Any,
        registry: Any,
        live_strategies: tuple[str, ...],
        kill_switch_available: Callable[[], bool] = lambda: False,
        clock_offset: Callable[[], float] = sntp_offset,
        policy: ReadinessPolicy = ReadinessPolicy(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._s, self._broker, self._store, self._health = settings, broker, store, health
        self._recovery, self._gate, self._engine, self._limits = recovery, gate, risk_engine, risk_limits
        self._registry, self._strategies = registry, live_strategies
        self._kill, self._offset, self._policy = kill_switch_available, clock_offset, policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self) -> ReadinessReport:
        checks = (
            self._guard("broker_connected", self._broker_connected),
            self._guard("account_verified", self._account_verified),
            self._guard("market_data_healthy", self._market_data),
            self._guard("database_healthy", self._database),
            self._guard("clock_synchronized", self._clock_sync),
            self._guard("strategy_validated", self._strategies_ok),
            self._guard("risk_limits_configured", self._risk_limits),
            self._guard("portfolio_reconciled", self._reconciled),
            self._guard("daily_loss_limit_configured", self._daily_loss),
            self._guard("max_position_configured", self._max_position),
            self._guard("emergency_shutdown_available", self._kill_switch),
            self._guard("paper_test_completed", self._paper_test),
            self._guard("configuration_validated", self._configuration),
        )
        report = ReadinessReport(self._clock(), checks)
        self._store.audit.append("readiness", "LIVE_READINESS_CHECK", "system", "live", {
            "ready": report.ready, "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks]})
        return report

    @staticmethod
    def _guard(name: str, fn: Callable[[], tuple[bool, str]]) -> CheckOutcome:
        try:
            passed, detail = fn()
        except Exception as exc:  # an unanswerable check is a failed check
            return CheckOutcome(name, False, f"check raised {type(exc).__name__}: {exc}")
        return CheckOutcome(name, bool(passed), detail)

    # ---- individual checks ----
    def _broker_connected(self) -> tuple[bool, str]:
        ok = bool(self._broker.is_connected)
        return ok, "connected" if ok else "broker is not connected"

    def _account_verified(self) -> tuple[bool, str]:
        account = self._broker.account()
        if account.base_currency.upper() != self._s.base_currency:
            return False, f"account currency {account.base_currency} != configured {self._s.base_currency}"
        if self._s.broker_account_id and account.account_id != self._s.broker_account_id:
            return False, "account id does not match BROKER_ACCOUNT_ID"
        if not account.equity > 0:
            return False, "account equity is not positive"
        return True, f"account {account.account_id} verified"

    def _market_data(self) -> tuple[bool, str]:
        if self._s.data_provider in _RESEARCH_ONLY:
            return False, f"data provider {self._s.data_provider!r} is research-only"
        stale = self._health.data_is_stale()
        return (not stale), "market data is stale or missing" if stale else "fresh"

    def _database(self) -> tuple[bool, str]:
        from .persistence.migrations import MIGRATIONS

        if not self._store.db.healthy():
            return False, "database not responding"
        applied = {r["version"] for r in self._store.db.query(
            "SELECT version FROM schema_migrations")}
        missing = {v for v, *_ in MIGRATIONS} - applied
        return (not missing), f"migrations pending: {sorted(missing)}" if missing else "healthy, schema current"

    def _clock_sync(self) -> tuple[bool, str]:
        offset = self._offset()
        ok = abs(offset) <= self._policy.max_clock_offset_seconds
        return ok, f"clock offset {offset:+.3f}s (limit {self._policy.max_clock_offset_seconds}s)"

    def _strategies_ok(self) -> tuple[bool, str]:
        if not self._strategies:
            return False, "no live strategies configured (LIVE_STRATEGIES)"
        problems = [p for s in self._strategies if (
            p := self._registry.check_live(s)) is not None]
        return (not problems), "; ".join(problems) if problems else f"{len(self._strategies)} strategy(ies) approved"

    def _risk_limits(self) -> tuple[bool, str]:
        disabled = self._engine.disabled_controls
        if disabled:
            return False, f"risk controls disabled: {sorted(disabled)}"
        return True, "all portfolio risk controls enforced"

    def _reconciled(self) -> tuple[bool, str]:
        report = self._recovery.reconcile()
        if not report.clean:
            return False, f"{len(report.discrepancies)} discrepancy(ies)"
        if self._gate.halted:
            return False, f"trading gate closed: {self._gate.blocked_reason()}"
        return True, "local state matches the broker"

    def _daily_loss(self) -> tuple[bool, str]:
        value = self._s.risk.max_daily_loss_pct
        ok = 0 < value <= 1 and "max_daily_loss_pct" not in self._engine.disabled_controls
        return ok, f"max daily loss {value:.2%}" if ok else "daily loss limit missing or disabled"

    def _max_position(self) -> tuple[bool, str]:
        ok = (self._limits.max_position_quantity > 0 and self._s.risk.max_position_notional_pct > 0
              and "max_position_notional_pct" not in self._engine.disabled_controls)
        return ok, "position limits set" if ok else "position limit missing or disabled"

    def _kill_switch(self) -> tuple[bool, str]:
        ok = bool(self._kill())
        return ok, "kill switch available" if ok else "no kill switch is configured"

    def _paper_test(self) -> tuple[bool, str]:
        stats = self._store.execution_records.paper_test_stats()
        ok = (stats["filled_orders"] >= self._policy.min_paper_filled_orders
              and stats["trading_days"] >= self._policy.min_paper_days)
        return ok, (f"{stats['filled_orders']} filled paper orders over {stats['trading_days']} day(s); "
                    f"need {self._policy.min_paper_filled_orders} over {self._policy.min_paper_days}")

    def _configuration(self) -> tuple[bool, str]:
        s, ex = self._s, self._s.execution
        problems = []
        if s.environment.value != "production":
            problems.append("APP_ENV is not production")
        if ex.mode is not TradingMode.LIVE or not ex.enable_live_trading:
            problems.append("live mode is not explicitly enabled")
        if ex.live_confirmation != LIVE_CONFIRMATION_PHRASE:
            problems.append("live confirmation phrase missing")
        if s.broker == "paper" or s.broker_api_key is None or s.broker_api_secret is None:
            problems.append("no real broker credentials configured")
        return (not problems), "; ".join(problems) if problems else "configuration valid"


def build_live_executor(checker: LiveReadinessChecker, broker: LiveBrokerClient, settings: ExecutionSettings,
                        *, trading_halted: Callable[[], bool]) -> LiveExecutor:
    """The only supported way to start live trading: run every check, refuse on any failure."""
    report = checker.run()
    if not report.ready:
        raise LiveTradingRefused(report.failures())
    return LiveExecutor(broker, settings, report.to_live_readiness(), trading_halted=trading_halted)
