"""Wires settings into a running API: paper trading only, since no live broker adapter exists yet."""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping

from fastapi import FastAPI

from ..core.brokers import PaperBroker
from ..core.executors import TradingMode
from ..core.models import AssetClass, Instrument, TradingStatus
from ..core.observability import CheckResult, ErrorCounter, HealthMonitor
from ..core.order_management import OrderManager
from ..core.persistence import SchemaOutOfDate, Store, open_store
from ..core.portfolio import PortfolioManager
from ..core.recovery import RecoveryManager, rebuild_portfolio, reconcile_positions
from ..core.risk import RiskEngine, RiskLimits
from ..core.learning import StrategyConfigRegistry
from ..core.kill_switch import AutoTriggerMonitor, AutoTriggerPolicy, KillSwitch
from ..core.observability import AlertManager, StructuredLogger
from ..core.live_readiness import LiveReadinessChecker
from ..core.markets import default_markets
from ..core.trading_gate import TradingGate
from ..core.security import SecurityError, get_secret
from ..core.settings import AppSettings, ConfigurationError, Environment, load_settings
from ..core.trading_service import TradingService
from .app import ApiContext, create_app


def instrument_from_row(row: Mapping[str, Any]) -> Instrument:
    extra = json.loads(row["payload"])
    hours = extra.get("trading_hours")
    return Instrument(
        instrument_id=row["instrument_id"], symbol=row["symbol"], exchange=row["exchange"],
        market=row["market"], asset_class=AssetClass(row["asset_class"]), currency=row["currency"],
        timezone=row["timezone"], tick_size=row["tick_size"], lot_size=row["lot_size"],
        trading_hours=tuple(time.fromisoformat(h)
                            for h in hours) if hours else None,
        price_precision=extra.get("price_precision", 2),
        minimum_order_quantity=extra.get("minimum_order_quantity", 1),
        shortable=extra.get("shortable"), trading_status=TradingStatus(row["trading_status"]))


def _parse_time(value: str, name: str, errors: list[str]) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        errors.append(f"{name} must be HH:MM, got {value!r}")
        return time(0, 0)


def build_context(
    settings: AppSettings,
    env: Mapping[str, str] | None = None,
    *,
    store: Store | None = None,
    market_stats: Callable[[str], Mapping[str, Any]] | None = None,
    sector_of: Callable[[str], str | None] | None = None,
) -> ApiContext:
    """`market_stats` supplies liquidity, slippage and correlation per instrument; without it, entries are rejected (fail closed)."""
    env = os.environ if env is None else env
    errors: list[str] = []
    needed = {n: (env.get(n) or "").strip() for n in
              ("ENTRY_WINDOW_START", "ENTRY_WINDOW_END", "MAX_POSITION_QUANTITY", "MAX_ORDER_NOTIONAL")}
    errors += [f"{n} is required" for n, v in needed.items() if not v]
    registry = default_markets()
    errors += [f"MARKETS lists unknown market {m!r}; known: {list(registry.codes())}"
               for m in settings.markets if m not in registry.codes()]
    token = None
    try:
        token = get_secret("API_TOKEN", env, required=settings.environment in (
            Environment.STAGING, Environment.PRODUCTION))
    except SecurityError as exc:
        errors.append(str(exc))
    if errors:
        raise ConfigurationError(errors)

    start = _parse_time(needed["ENTRY_WINDOW_START"],
                        "ENTRY_WINDOW_START", errors)
    end = _parse_time(needed["ENTRY_WINDOW_END"], "ENTRY_WINDOW_END", errors)
    try:
        max_qty, max_notional = int(needed["MAX_POSITION_QUANTITY"]), float(
            needed["MAX_ORDER_NOTIONAL"])
        limits = RiskLimits(
            max_position_quantity=max_qty, max_order_notional=max_notional,
            max_open_positions=settings.risk.max_open_positions,
            max_trades_per_day=settings.risk.max_trades_per_day, cash_requirement_rate=1.0,
            entry_window=(start, end),
            max_market_data_age=timedelta(seconds=settings.max_market_data_age_seconds))
    except (TypeError, ValueError) as exc:
        errors.append(f"invalid risk limit: {exc}")
    if errors:
        raise ConfigurationError(errors)

    # Staging/production never migrate implicitly; run `python -m stockmarket.ops migrate` first.
    auto_migrate = (env.get("AUTO_MIGRATE") or (
        "false" if settings.environment in (Environment.STAGING, Environment.PRODUCTION) else "true")).lower() == "true"
    try:
        store = store or open_store(
            settings.database_url.reveal(), migrate_schema=auto_migrate)
    except SchemaOutOfDate as exc:
        raise ConfigurationError([str(exc)]) from exc
    instruments = {r["instrument_id"]: instrument_from_row(
        r) for r in store.instruments.list()}
    fx_rates: dict[str, float] = {}
    for pair in (env.get("FX_RATES") or "").split(","):
        if "=" in pair:
            ccy, rate = pair.split("=", 1)
            fx_rates[ccy.strip().upper()] = float(rate)
    portfolio = rebuild_portfolio(store, instruments, settings.base_currency,
                                  float(env.get("PAPER_STARTING_CASH") or 100_000), fx_rates)
    portfolio.on_fill = lambda fill: store.fills.save(fill)
    broker = PaperBroker(portfolio, instruments, market_status_fn=lambda m: registry.is_regular_session(
        m, datetime.now(timezone.utc)))
    gate = TradingGate()
    order_manager = OrderManager(broker)
    strategies = StrategyConfigRegistry(store.strategy_configs)
    risk_engine = RiskEngine(limits, settings.risk.to_portfolio_limits())
    paper = TradingService(
        mode=TradingMode.PAPER, risk_engine=risk_engine,
        order_manager=order_manager, portfolio=portfolio, instruments=instruments,
        quotes=broker.last_quote, market_stats=market_stats, sector_of=sector_of, store=store, gate=gate,
        strategy_approval=strategies.check_live, provenance_source=strategies.version_info)
    recovery = RecoveryManager(store=store, order_manager=order_manager, portfolio=portfolio,
                               broker=broker, gate=gate)
    # connects the broker, restores orders, and halts entries on any discrepancy
    recovery.recover()

    health = HealthMonitor(max_market_data_age=timedelta(seconds=settings.max_market_data_age_seconds),
                           errors=ErrorCounter())
    health.register_check("database", store.db.healthy)
    health.register_check("broker", lambda: broker.is_connected)
    health.register_check("trading_gate", lambda: CheckResult(
        not gate.halted, gate.blocked_reason() or "open"))
    live_strategies = tuple(s.strip() for s in (
        env.get("LIVE_STRATEGIES") or "").split(",") if s.strip())
    logger = StructuredLogger("platform", errors=health.errors)
    alerts = AlertManager(logger)
    kill_switch = KillSwitch(gate, services=lambda: [
                             paper], store=store, logger=logger, alerts=alerts)
    kill_switch.restore()  # an engaged kill switch survives restarts
    monitor = AutoTriggerMonitor(
        kill_switch, portfolio=portfolio, health=health,
        policy=AutoTriggerPolicy(
            settings.risk.max_daily_loss_pct, settings.risk.max_drawdown_pct),
        data_expected=lambda: any(registry.is_regular_session(
            m, datetime.now(timezone.utc)) for m in settings.markets),
        unexpected_positions=lambda: [
            d.subject for d in reconcile_positions(portfolio, broker)],
        cancel_open_orders=(env.get("KILL_SWITCH_CANCEL_ORDERS") or "").lower() == "true")
    if (env.get("KILL_SWITCH_AUTO") or "").lower() == "true":
        monitor.start(float(env.get("KILL_SWITCH_INTERVAL_SECONDS") or 5))
    checker = LiveReadinessChecker(
        settings=settings, broker=broker, store=store, health=health, recovery=recovery, gate=gate,
        risk_engine=risk_engine, risk_limits=limits, registry=strategies, live_strategies=live_strategies,
        kill_switch_available=kill_switch.available)
    return ApiContext(settings=settings, paper=paper, live=None, store=store, health=health,
                      api_token=token, gate=gate, recovery=recovery, markets=registry, readiness=checker,
                      kill_switch=kill_switch, monitor=monitor)


def create_app_from_env() -> FastAPI:
    """uvicorn stockmarket.api.bootstrap:create_app_from_env --factory"""
    settings = load_settings()
    return create_app(build_context(settings))
