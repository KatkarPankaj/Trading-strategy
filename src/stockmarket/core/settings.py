"""Typed, environment-aware application settings that fail fast when required configuration is missing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .executors import ExecutionConfigError, ExecutionSettings, TradingMode
from .risk_portfolio import PortfolioRiskLimits
from .security import (
    Secret,
    SecurityError,
    assert_no_secrets_in_config,
    get_secret,
    redact_dsn,
)


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PAPER = "paper"
    STAGING = "staging"
    PRODUCTION = "production"


_STRICT_ENVS = frozenset({Environment.STAGING, Environment.PRODUCTION})
_RESEARCH_ONLY_PROVIDERS = frozenset({"yahoo", "mock"})
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigurationError(Exception):
    """Carries every problem found, so one run reports all of them."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(errors))


@dataclass(frozen=True, slots=True)
class RiskSettings:
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_position_notional_pct: float
    max_total_notional_pct: float
    max_sector_exposure_pct: float
    max_correlated_exposure_pct: float
    max_leverage: float
    max_open_positions: int
    max_trades_per_day: int
    max_orders_per_minute: int
    min_liquidity: float
    max_slippage_bps: float

    def to_portfolio_limits(self) -> PortfolioRiskLimits:
        return PortfolioRiskLimits(
            max_risk_per_trade_pct=self.risk_per_trade_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            max_position_notional_pct=self.max_position_notional_pct,
            max_total_notional_pct=self.max_total_notional_pct,
            max_sector_exposure_pct=self.max_sector_exposure_pct,
            max_correlated_exposure_pct=self.max_correlated_exposure_pct,
            max_leverage=self.max_leverage,
            max_orders_per_minute=self.max_orders_per_minute,
            min_liquidity=self.min_liquidity,
            max_slippage_bps=self.max_slippage_bps,
            duplicate_order_prevention=True,
        )


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: Environment
    execution: ExecutionSettings
    database_url: Secret
    base_currency: str
    markets: tuple[str, ...]
    data_provider: str
    broker: str
    broker_api_key: Secret | None
    broker_api_secret: Secret | None
    broker_account_id: str | None
    risk: RiskSettings
    log_level: str
    max_market_data_age_seconds: float

    def to_public_dict(self) -> dict[str, Any]:
        """Safe to log or show in a UI: secrets are masked."""
        return {
            "environment": self.environment.value,
            "trading_mode": self.execution.mode.value,
            "live_trading_enabled": self.execution.enable_live_trading,
            "database_url": redact_dsn(self.database_url.reveal()),
            "base_currency": self.base_currency,
            "markets": list(self.markets),
            "data_provider": self.data_provider,
            "broker": self.broker,
            "broker_credentials_configured": self.broker_api_key is not None and self.broker_api_secret is not None,
            "risk": {k: getattr(self.risk, k) for k in self.risk.__slots__},
            "log_level": self.log_level,
            "max_market_data_age_seconds": self.max_market_data_age_seconds,
        }


class _Reader:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = values
        self.errors: list[str] = []

    def text(self, name: str, default: str | None = None, *, required: bool = False) -> str | None:
        raw = (self.values.get(name) or "").strip()
        if raw:
            return raw
        if required:
            self.errors.append(f"{name} is required")
        return default

    def number(self, name: str, default: float | None, *, lo: float = 0.0, hi: float | None = None,
               integer: bool = False, required: bool = False) -> float | int:
        raw = self.text(name, None, required=required)
        if raw is None:
            return default if default is not None else 0
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            self.errors.append(
                f"{name} must be a {'whole number' if integer else 'number'}, got {raw!r}")
            return default or 0
        if value != value or value in (float("inf"), float("-inf")) or value <= lo or (hi is not None and value > hi):
            bound = f"greater than {lo}" + \
                (f" and at most {hi}" if hi is not None else "")
            self.errors.append(f"{name} must be {bound}, got {raw!r}")
        return value


_DEV_RISK = dict(
    RISK_PER_TRADE_PCT=0.005, MAX_DAILY_LOSS_PCT=0.02, MAX_DRAWDOWN_PCT=0.10,
    MAX_POSITION_NOTIONAL_PCT=0.20, MAX_TOTAL_NOTIONAL_PCT=1.0, MAX_SECTOR_EXPOSURE_PCT=0.30,
    MAX_CORRELATED_EXPOSURE_PCT=0.40, MAX_LEVERAGE=1.0, MAX_OPEN_POSITIONS=5,
    MAX_TRADES_PER_DAY=10, MAX_ORDERS_PER_MINUTE=10, MIN_LIQUIDITY=1_000_000.0, MAX_SLIPPAGE_BPS=20.0,
)


def load_settings(
    env: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
) -> AppSettings:
    """Environment variables override an optional JSON file of non-secret values (flat, upper-case keys)."""
    process_env = os.environ if env is None else env
    values: dict[str, str] = {}
    if config_file is not None:
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        assert_no_secrets_in_config(data, str(config_file))
        values.update({k.upper(): str(v) for k, v in data.items()})
    values.update({k: v for k, v in process_env.items() if v != ""})

    r = _Reader(values)
    try:
        environment = Environment(
            (r.text("APP_ENV", "development") or "").lower())
    except ValueError:
        r.errors.append(
            f"APP_ENV must be one of {[e.value for e in Environment]}")
        environment = Environment.DEVELOPMENT
    strict = environment in _STRICT_ENVS

    try:
        execution = ExecutionSettings.from_env(values)
    except ExecutionConfigError as exc:
        r.errors.append(str(exc))
        execution = ExecutionSettings()
    if execution.mode is TradingMode.LIVE and environment is not Environment.PRODUCTION:
        r.errors.append(
            "TRADING_MODE=live is only allowed when APP_ENV=production")

    default_db = "sqlite://:memory:" if environment is Environment.TEST else "sqlite:///outputs/trading.db"
    if not values.get("DATABASE_URL") and values.get("DATABASE_URL_FILE"):
        # container secret: the URL (with password) is read from a file, not the environment
        try:
            values["DATABASE_URL"] = get_secret(
                "DATABASE_URL", values).reveal()  # type: ignore[union-attr]
        except SecurityError as exc:
            r.errors.append(str(exc))
    database_url = r.text(
        "DATABASE_URL", None if strict else default_db, required=strict)
    if database_url:
        scheme = urlparse(database_url).scheme
        if scheme not in ("sqlite", "postgresql", "postgres"):
            r.errors.append(f"DATABASE_URL has unsupported scheme {scheme!r}")
        elif strict and scheme == "sqlite":
            r.errors.append(
                f"DATABASE_URL must be PostgreSQL when APP_ENV={environment.value}")

    base_currency = (r.text("BASE_CURRENCY", "USD") or "USD").upper()
    if not (len(base_currency) == 3 and base_currency.isalpha()):
        r.errors.append("BASE_CURRENCY must be a 3-letter currency code")
    markets = tuple(m.strip().upper() for m in (
        r.text("MARKETS", "US") or "").split(",") if m.strip())
    if not markets:
        r.errors.append("MARKETS must list at least one market")

    data_provider = (r.text("DATA_PROVIDER", "mock") or "mock").lower()
    broker = (r.text("BROKER", "paper") or "paper").lower()
    if execution.mode is TradingMode.LIVE:
        if broker == "paper":
            r.errors.append(
                "BROKER must be a real broker when TRADING_MODE=live")
        if data_provider in _RESEARCH_ONLY_PROVIDERS:
            r.errors.append(
                f"DATA_PROVIDER={data_provider} is research-only and cannot feed live trading")

    api_key = api_secret = None
    needs_credentials = broker != "paper" and (
        execution.mode is TradingMode.LIVE or strict)
    try:
        api_key = get_secret("BROKER_API_KEY", values,
                             required=needs_credentials)
        api_secret = get_secret("BROKER_API_SECRET",
                                values, required=needs_credentials)
    except SecurityError as exc:
        r.errors.append(str(exc))

    risk_values = {}
    for name, default in _DEV_RISK.items():
        integer = name in ("MAX_OPEN_POSITIONS",
                           "MAX_TRADES_PER_DAY", "MAX_ORDERS_PER_MINUTE")
        hi = 1.0 if name.endswith("_PCT") and name not in (
            "MAX_TOTAL_NOTIONAL_PCT",) else None
        risk_values[name.lower()] = r.number(
            name, None if strict else default, hi=hi, integer=integer, required=strict)
    risk = RiskSettings(**risk_values)  # type: ignore[arg-type]

    log_level = (r.text("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in _LOG_LEVELS:
        r.errors.append(f"LOG_LEVEL must be one of {list(_LOG_LEVELS)}")
    max_age = float(r.number("MAX_MARKET_DATA_AGE_SECONDS", 300.0))

    if r.errors:
        raise ConfigurationError(r.errors)
    assert database_url is not None
    return AppSettings(
        environment=environment, execution=execution, database_url=Secret(
            database_url),
        base_currency=base_currency, markets=markets, data_provider=data_provider, broker=broker,
        broker_api_key=api_key, broker_api_secret=api_secret,
        broker_account_id=r.text("BROKER_ACCOUNT_ID"), risk=risk, log_level=log_level,
        max_market_data_age_seconds=max_age,
    )
