"""Paper and live execution paths; paper is the default and live needs several explicit gates."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from enum import Enum
from threading import RLock
from typing import Callable, Mapping, Protocol

from .order_management import ManagedOrder

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_LIVE_TRADING_RISK"


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class ExecutionConfigError(ValueError):
    """Execution environment variables are malformed."""


class LiveTradingRefused(RuntimeError):
    """Live trading preconditions were not met; carries every failed condition."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = tuple(failures)
        super().__init__("Live trading refused: " + "; ".join(failures))


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    mode: TradingMode = TradingMode.PAPER
    enable_live_trading: bool = False
    live_confirmation: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExecutionSettings":
        """Unset values mean PAPER with live disabled; malformed values raise instead of guessing."""
        env = os.environ if env is None else env
        raw_mode = (env.get("TRADING_MODE") or "paper").strip().lower()
        if raw_mode not in ("paper", "live"):
            raise ExecutionConfigError(
                f"TRADING_MODE must be 'paper' or 'live', got {raw_mode!r}")
        raw_enable = (env.get("ENABLE_LIVE_TRADING")
                      or "false").strip().lower()
        if raw_enable not in ("true", "false"):
            raise ExecutionConfigError(
                f"ENABLE_LIVE_TRADING must be 'true' or 'false', got {raw_enable!r}")
        return cls(
            mode=TradingMode(raw_mode.upper()),
            enable_live_trading=raw_enable == "true",
            live_confirmation=env.get("LIVE_TRADING_CONFIRMATION") or None,
        )


@dataclass(frozen=True, slots=True)
class LiveReadiness:
    """Each field must be affirmatively True; the default is not ready."""

    broker_connected: bool = False
    account_verified: bool = False
    market_data_healthy: bool = False
    database_healthy: bool = False
    clock_synchronized: bool = False
    strategy_validated: bool = False
    risk_limits_configured: bool = False
    portfolio_reconciled: bool = False
    daily_loss_limit_configured: bool = False
    max_position_configured: bool = False
    emergency_shutdown_available: bool = False
    paper_test_completed: bool = False
    configuration_validated: bool = False


def live_trading_failures(settings: ExecutionSettings, readiness: LiveReadiness) -> list[str]:
    failures = []
    if settings.mode is not TradingMode.LIVE:
        failures.append("TRADING_MODE is not live")
    if settings.enable_live_trading is not True:
        failures.append("ENABLE_LIVE_TRADING is not true")
    if settings.live_confirmation != LIVE_CONFIRMATION_PHRASE:
        failures.append("LIVE_TRADING_CONFIRMATION is missing or incorrect")
    for f in fields(readiness):
        if getattr(readiness, f.name) is not True:
            failures.append(f"readiness check failed: {f.name}")
    return failures


class Executor(Protocol):
    """Gateway used by OrderManager; paper and live implementations share nothing but this shape."""

    mode: TradingMode

    def submit(self, order: ManagedOrder) -> str: ...
    def cancel(self, broker_order_id: str) -> None: ...


class PaperExecutor:
    """Simulated execution: accepts orders locally and never touches a broker."""

    mode = TradingMode.PAPER

    def __init__(self) -> None:
        self._seq = 0
        self._open: dict[str, ManagedOrder] = {}
        self._lock = RLock()

    def submit(self, order: ManagedOrder) -> str:
        with self._lock:
            self._seq += 1
            broker_id = f"PAPER-{self._seq}"
            self._open[broker_id] = order
            return broker_id

    def cancel(self, broker_order_id: str) -> None:
        with self._lock:
            if self._open.pop(broker_order_id, None) is None:
                raise KeyError(f"unknown paper order {broker_order_id}")


class LiveBrokerClient(Protocol):
    def submit_order(self, order: ManagedOrder) -> str: ...
    def cancel_order(self, broker_order_id: str) -> None: ...


class LiveExecutor:
    """Sends real orders; cannot be constructed unless every live gate passes."""

    mode = TradingMode.LIVE

    def __init__(
        self,
        broker: LiveBrokerClient,
        settings: ExecutionSettings,
        readiness: LiveReadiness,
        *,
        trading_halted: Callable[[], bool],
    ) -> None:
        failures = live_trading_failures(settings, readiness)
        if failures:
            raise LiveTradingRefused(failures)
        self._broker = broker
        self._halted = trading_halted

    def submit(self, order: ManagedOrder) -> str:
        if self._halted():
            raise LiveTradingRefused(["trading is halted (kill switch)"])
        return self._broker.submit_order(order)

    def cancel(self, broker_order_id: str) -> None:
        # cancels stay allowed while halted
        self._broker.cancel_order(broker_order_id)


def create_executor(
    settings: ExecutionSettings | None = None,
    *,
    broker: LiveBrokerClient | None = None,
    readiness: LiveReadiness | None = None,
    trading_halted: Callable[[], bool] | None = None,
) -> Executor:
    """Paper unless live was explicitly requested; a refused live request raises, never downgrades silently."""
    settings = settings or ExecutionSettings()
    if settings.mode is TradingMode.PAPER:
        return PaperExecutor()
    if broker is None or trading_halted is None:
        raise LiveTradingRefused(
            ["live mode requires a broker client and a kill-switch callback"])
    return LiveExecutor(broker, settings, readiness or LiveReadiness(),
                        trading_halted=trading_halted)
