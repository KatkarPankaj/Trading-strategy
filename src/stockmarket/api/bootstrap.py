"""Wires settings into a running API: paper trading only, since no live broker adapter exists yet."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping

from fastapi import FastAPI

from ..core.brokers import PaperBroker
from ..core.executors import TradingMode
from ..core.models import AssetClass, Instrument, TradingStatus
from ..core.observability import CheckResult, ErrorCounter, HealthMonitor
from ..core.order_management import OrderManager
from ..core.market_session import MarketSession
from ..core.ai import AIAnalyst
from ..core.ai.candidate_assessment import CandidateAssessmentService
from ..core.autonomous_research import (
    AutonomousResearchService,
    parse_autonomous_research_settings,
)
from ..core.signal_generation import (
    SignalGenerationService,
    parse_signal_generation_settings,
)
from ..core.trade_proposals import TradeProposalService, TradeProposalSettings
from ..core.paper_lifecycle import (
    PaperPositionManager,
    PaperProposalExecutionService,
)
from ..core.ai.openai_compatible import OpenAICompatibleProvider
from ..core.persistence import SchemaOutOfDate, Store, open_store
from ..core.recovery import RecoveryManager, rebuild_portfolio, reconcile_positions
from ..core.risk import RiskEngine, RiskLimits
from ..core.learning import StrategyConfigRegistry
from ..core.strategies import OrbVwapStrategy, Strategy
from ..core.strategy_pipeline import StrategyResearchPipeline
from ..core.market_intelligence import MarketIntelligenceOrchestrator
from ..core.candidate_research import (
    CandidateResearchService,
    parse_candidate_research_settings,
)
from ..core.research import FundamentalEvidenceProducer, SectorEvidenceProducer
from ..core.kill_switch import AutoTriggerMonitor, AutoTriggerPolicy, KillSwitch
from ..core.observability import AlertManager, StructuredLogger
from ..core.live_readiness import LiveReadinessChecker
from ..core.markets import MarketRegistry, default_markets
from ..core.data import DataPolicy, ResilientProvider, create_market_data_provider, quote_source
from ..core.scanner import (
    MarketScanner,
    RegistryUniverseProvider,
    StaticUniverseProvider,
    parse_scanner_settings,
    parse_universe_definitions,
)
from ..core.sizing import BrokerConstraints, SizingLimits
from ..core.data.yahoo_fundamentals import YahooEarningsObservationProvider
from ..core.data.nse_sector_indices import NSESectorIndexObservationProvider
from ..news import FinnhubNewsProvider
from ..core.research import NewsEvidenceProducer
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
        shortable=extra.get("shortable"), trading_status=TradingStatus(row["trading_status"]),
        name=extra.get("name"), country=extra.get("country"), sector=extra.get("sector"),
        industry=extra.get("industry"), isin=extra.get("isin"), figi=extra.get("figi"),
        cusip=extra.get("cusip"), mic=extra.get("mic"),
        exchange_symbol=extra.get("exchange_symbol"),
        provider_symbol=extra.get("provider_symbol"),
        active=extra.get("active", True), tradable=extra.get("tradable", True),
        market_cap=extra.get("market_cap"))


def _parse_time(value: str, name: str, errors: list[str]) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        errors.append(f"{name} must be HH:MM, got {value!r}")
        return time(0, 0)


def _research_from_env(
    env: Mapping[str, str],
    registry: MarketRegistry,
    configured_markets: tuple[str, ...],
) -> tuple[AIAnalyst | None, dict[str, MarketSession], list[str]]:
    errors: list[str] = []
    base_url = (env.get("AI_BASE_URL") or "").strip()
    model = (env.get("AI_MODEL") or "").strip()
    api_key_value = (env.get("AI_API_KEY") or "").strip()
    api_key_file = (env.get("AI_API_KEY_FILE") or "").strip()
    timeout_value = (env.get("AI_TIMEOUT_SECONDS") or "").strip()
    ai_configured = any((base_url, model, api_key_value, timeout_value))
    analyst: AIAnalyst | None = None

    if ai_configured:
        if not base_url or not model or not (api_key_value or api_key_file):
            errors.append(
                "AI_BASE_URL, AI_MODEL, and AI_API_KEY or AI_API_KEY_FILE must be configured together")
        else:
            try:
                key = get_secret("AI_API_KEY", env)
                if key is None:
                    raise SecurityError("AI_API_KEY is required")
                timeout = float(timeout_value or "20")
                provider = OpenAICompatibleProvider(
                    base_url, model, key, timeout=timeout)
                analyst = AIAnalyst(provider)
            except (SecurityError, TypeError, ValueError) as exc:
                errors.append(f"invalid AI provider configuration: {exc}")

    sessions: dict[str, MarketSession] = {}
    session_data = (env.get("RESEARCH_SESSIONS") or "").strip()
    if session_data:
        try:
            parsed = json.loads(session_data)
        except json.JSONDecodeError:
            errors.append("RESEARCH_SESSIONS must be valid JSON")
            parsed = None
        if parsed is not None:
            if not isinstance(parsed, dict):
                errors.append("RESEARCH_SESSIONS must be an object keyed by market code")
            else:
                for market_code, settings in parsed.items():
                    if not isinstance(market_code, str) or not isinstance(settings, dict):
                        errors.append("RESEARCH_SESSIONS entries must map market codes to objects")
                        continue
                    code = market_code.upper()
                    if code not in configured_markets:
                        errors.append(
                            f"RESEARCH_SESSIONS market {code!r} must be listed in MARKETS")
                        continue
                    if not set(settings).issubset({
                            "opening_range_minutes", "entry_start", "entry_cutoff",
                            "square_off", "late_entry_start"}):
                        errors.append(
                            f"RESEARCH_SESSIONS for {code} contains unsupported fields")
                        continue
                    required = {
                        "opening_range_minutes", "entry_cutoff",
                        "square_off", "late_entry_start",
                    }
                    if not required.issubset(settings):
                        errors.append(
                            f"RESEARCH_SESSIONS for {code} requires "
                            "opening_range_minutes, entry_cutoff, square_off, and late_entry_start")
                        continue
                    definition = registry.get(code)
                    try:
                        minutes = settings["opening_range_minutes"]
                        if isinstance(minutes, bool) or not isinstance(minutes, int) \
                                or not 1 <= minutes <= 240:
                            raise ValueError("opening_range_minutes must be between 1 and 240")
                        open_at = definition.calendar.open_time
                        close_at = definition.calendar.close_time
                        opening_end = (datetime.combine(
                            date.min, open_at) + timedelta(minutes=minutes)).time()
                        session = MarketSession(
                            timezone=definition.timezone,
                            market_open=open_at,
                            opening_range_end=opening_end,
                            entry_cutoff=time.fromisoformat(settings["entry_cutoff"]),
                            square_off=time.fromisoformat(settings["square_off"]),
                            market_close=close_at,
                            entry_start=time.fromisoformat(settings["entry_start"])
                            if "entry_start" in settings else None,
                            late_entry_start=time.fromisoformat(
                                settings["late_entry_start"]),
                        )
                    except (TypeError, ValueError) as exc:
                        errors.append(f"invalid RESEARCH_SESSIONS for {code}: {exc}")
                        continue
                    sessions[code] = session
    elif ai_configured and analyst is not None:
        errors.append(
            "RESEARCH_SESSIONS is required when an AI provider is configured")

    return analyst, sessions, errors


def create_candidate_assessment_service(
    store: Store,
    instruments: Mapping[str, Instrument],
    registry: MarketRegistry,
    *,
    env: Mapping[str, str] | None = None,
    research_analyst: AIAnalyst | None = None,
    research_strategies: Mapping[str, Strategy] | None = None,
) -> CandidateAssessmentService | None:
    """Create the snapshot-only AI service without starting data or execution services."""
    env = os.environ if env is None else env
    if research_analyst is None:
        research_analyst, _, errors = _research_from_env(
            env, registry, tuple(sorted({i.market for i in instruments.values()})))
        if errors:
            raise ConfigurationError("; ".join(errors))
    if research_analyst is None:
        return None
    return CandidateAssessmentService(
        research_analyst,
        store.research_runs,
        instruments,
        registry,
        research_strategies or {"orb_vwap": OrbVwapStrategy()},
        model_version=(env.get("AI_MODEL") or "unspecified").strip(),
    )


def build_context(
    settings: AppSettings,
    env: Mapping[str, str] | None = None,
    *,
    store: Store | None = None,
    market_stats: Callable[[str], Mapping[str, Any]] | None = None,
    sector_of: Callable[[str], str | None] | None = None,
    research_analyst: AIAnalyst | None = None,
    research_strategies: Mapping[str, Strategy] | None = None,
    research_sessions: Mapping[str, MarketSession] | None = None,
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
    env_analyst, env_sessions, research_errors = _research_from_env(
        env, registry, settings.markets)
    errors.extend(research_errors)
    if research_analyst is None:
        research_analyst = env_analyst
    if research_sessions is None:
        research_sessions = env_sessions
    fundamental_provider_name = (env.get("FUNDAMENTAL_PROVIDER") or "").strip().lower()
    if fundamental_provider_name not in ("", "yahoo"):
        errors.append("FUNDAMENTAL_PROVIDER must be 'yahoo' when set")
    if fundamental_provider_name and research_analyst is None:
        errors.append("FUNDAMENTAL_PROVIDER requires a configured AI research provider")
    sector_provider_name = (env.get("SECTOR_PROVIDER") or "").strip().lower()
    if sector_provider_name not in ("", "nse"):
        errors.append("SECTOR_PROVIDER must be 'nse' when set")
    sector_map: dict[str, str] = {}
    sector_map_value = (env.get("NSE_SECTOR_INDEX_MAP") or "").strip()
    if sector_map_value:
        try:
            raw_sector_map = json.loads(sector_map_value)
        except json.JSONDecodeError:
            errors.append("NSE_SECTOR_INDEX_MAP must be valid JSON")
        else:
            if not isinstance(raw_sector_map, dict):
                errors.append("NSE_SECTOR_INDEX_MAP must be a JSON object")
            else:
                for symbol, index_name in raw_sector_map.items():
                    if not isinstance(symbol, str) or not symbol.strip() \
                            or not isinstance(index_name, str) or not index_name.strip():
                        errors.append(
                            "NSE_SECTOR_INDEX_MAP must map non-empty symbols to index names")
                        break
                    if not index_name.strip().upper().startswith("NIFTY "):
                        errors.append(
                            "NSE_SECTOR_INDEX_MAP values must be NSE NIFTY index names")
                        break
                    normalized_symbol = symbol.strip().upper()
                    if normalized_symbol in sector_map:
                        errors.append(
                            f"NSE_SECTOR_INDEX_MAP contains duplicate symbol "
                            f"{normalized_symbol!r}")
                        break
                    sector_map[normalized_symbol] = " ".join(index_name.split())
    if sector_provider_name == "nse" and not sector_map:
        errors.append(
            "NSE_SECTOR_INDEX_MAP is required when SECTOR_PROVIDER=nse")
    if sector_provider_name == "nse" and "IN" not in settings.markets:
        errors.append("MARKETS must include IN when SECTOR_PROVIDER=nse")
    if sector_map and sector_provider_name != "nse":
        errors.append("NSE_SECTOR_INDEX_MAP requires SECTOR_PROVIDER=nse")
    if sector_provider_name and research_analyst is None:
        errors.append("SECTOR_PROVIDER requires a configured AI research provider")
    news_provider_name = (env.get("NEWS_PROVIDER") or "").strip().lower()
    if news_provider_name not in ("", "finnhub"):
        errors.append("NEWS_PROVIDER must be 'finnhub' when set")
    finnhub_symbol_map: dict[str, str] = {}
    finnhub_symbol_map_value = (env.get("FINNHUB_SYMBOL_MAP") or "").strip()
    if finnhub_symbol_map_value:
        try:
            raw_finnhub_symbol_map = json.loads(finnhub_symbol_map_value)
        except json.JSONDecodeError:
            errors.append("FINNHUB_SYMBOL_MAP must be valid JSON")
        else:
            if not isinstance(raw_finnhub_symbol_map, dict):
                errors.append("FINNHUB_SYMBOL_MAP must be a JSON object")
            else:
                for key, provider_symbol in raw_finnhub_symbol_map.items():
                    if not isinstance(key, str) or ":" not in key \
                            or not isinstance(provider_symbol, str) \
                            or not provider_symbol.strip():
                        errors.append(
                            "FINNHUB_SYMBOL_MAP must map MARKET:SYMBOL to provider symbols")
                        break
                    market_code, symbol = key.split(":", 1)
                    market_code = market_code.strip().upper()
                    normalized_key = f"{market_code}:{symbol.strip().upper()}"
                    if not symbol.strip() or market_code not in registry.codes():
                        errors.append(
                            f"FINNHUB_SYMBOL_MAP contains invalid key {key!r}")
                        break
                    if market_code not in settings.markets:
                        errors.append(
                            f"FINNHUB_SYMBOL_MAP market {market_code!r} "
                            "must be included in MARKETS")
                        break
                    if normalized_key in finnhub_symbol_map:
                        errors.append(
                            f"FINNHUB_SYMBOL_MAP contains duplicate key {normalized_key!r}")
                        break
                    finnhub_symbol_map[normalized_key] = provider_symbol.strip().upper()
    if news_provider_name == "finnhub":
        try:
            finnhub_api_key = get_secret("FINNHUB_API_KEY", env)
            if finnhub_api_key is None:
                errors.append(
                    "FINNHUB_API_KEY or FINNHUB_API_KEY_FILE is required "
                    "when NEWS_PROVIDER=finnhub")
        except SecurityError as exc:
            errors.append(str(exc))
            finnhub_api_key = None
        if research_analyst is None:
            errors.append(
                "NEWS_PROVIDER=finnhub requires a configured AI research provider")
    else:
        finnhub_api_key = None
        if finnhub_symbol_map:
            errors.append("FINNHUB_SYMBOL_MAP requires NEWS_PROVIDER=finnhub")
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
        sizing_limits = SizingLimits(
            risk_per_trade_pct=settings.risk.risk_per_trade_pct,
            max_order_notional=max_notional,
            max_position_notional_pct=settings.risk.max_position_notional_pct,
            max_total_notional_pct=settings.risk.max_total_notional_pct,
            max_sector_exposure_pct=settings.risk.max_sector_exposure_pct,
            broker=BrokerConstraints(
                max_order_quantity=max_qty, max_order_notional=max_notional),
        )
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
    scanner_settings = parse_scanner_settings(
        (env.get("SCANNER_SETTINGS") or "").strip())
    configured_universes = (env.get("SCANNER_UNIVERSES") or "").strip()
    if configured_universes:
        definitions = parse_universe_definitions(configured_universes)
        for definition in definitions:
            if not set(market.upper() for market in definition.markets).issubset(
                    set(settings.markets)):
                raise ValueError(
                    f"universe {definition.universe_id!r} contains a market not enabled in MARKETS")
        universe_provider = StaticUniverseProvider(definitions, instruments)
    else:
        universe_provider = RegistryUniverseProvider(instruments, settings.markets)
    fx_rates: dict[str, float] = {}
    for pair in (env.get("FX_RATES") or "").split(","):
        if "=" in pair:
            ccy, rate = pair.split("=", 1)
            fx_rates[ccy.strip().upper()] = float(rate)
    portfolio = rebuild_portfolio(store, instruments, settings.base_currency,
                                  float(env.get("PAPER_STARTING_CASH") or 100_000), fx_rates)
    def persist_paper_fill(fill: Any) -> None:
        store.fills.save(fill)
        store.positions.replace_all(
            portfolio.positions().values(), fill.timestamp)

    portfolio.on_fill = persist_paper_fill
    broker = PaperBroker(portfolio, instruments, market_status_fn=lambda m: registry.is_regular_session(
        m, datetime.now(timezone.utc)))
    gate = TradingGate()
    order_manager = OrderManager(broker)
    strategies = StrategyConfigRegistry(store.strategy_configs)
    risk_engine = RiskEngine(limits, settings.risk.to_portfolio_limits())
    health = HealthMonitor(max_market_data_age=timedelta(seconds=settings.max_market_data_age_seconds),
                           errors=ErrorCounter())
    health.register_check("database", store.db.healthy)
    health.register_check("broker", lambda: broker.is_connected)
    health.register_check("trading_gate", lambda: CheckResult(
        not gate.halted, gate.blocked_reason() or "open"))
    logger = StructuredLogger("platform", errors=health.errors)
    raw_market_data = create_market_data_provider(
        settings.data_provider, instruments, markets=registry)
    market_data = ResilientProvider(
        raw_market_data,
        policy=DataPolicy(max_quote_age=timedelta(
            seconds=settings.max_market_data_age_seconds)),
        health=health,
        logger=logger,
    )
    fundamental_observation_provider = (
        YahooEarningsObservationProvider()
        if fundamental_provider_name == "yahoo" else None)
    sector_observation_provider = (
        NSESectorIndexObservationProvider(sector_map)
        if sector_provider_name == "nse" and sector_map else None)
    raw_news_provider = (
        FinnhubNewsProvider(finnhub_api_key.reveal(), symbol_map=finnhub_symbol_map)
        if news_provider_name == "finnhub" and finnhub_api_key is not None else None)
    scanner = MarketScanner(
        market_data, universe_provider, registry, repository=store.scanner_runs,
        settings=scanner_settings,
    )
    candidate_research = CandidateResearchService(
        market_data,
        store.scanner_runs,
        store.research_runs,
        instruments,
        news_provider=raw_news_provider,
        fundamental_provider=fundamental_observation_provider,
        sector_provider=sector_observation_provider,
        settings=parse_candidate_research_settings(
            (env.get("CANDIDATE_RESEARCH_SETTINGS") or "").strip()),
    )
    registered_research_strategies = (
        research_strategies or {"orb_vwap": OrbVwapStrategy()})
    candidate_assessment = (
        create_candidate_assessment_service(
            store,
            instruments,
            registry,
            env=env,
            research_analyst=research_analyst,
            research_strategies=registered_research_strategies,
        )
        if research_analyst is not None else None
    )
    autonomous_research = None
    if candidate_assessment is not None:
        autonomous_settings = parse_autonomous_research_settings(
            (env.get("AUTONOMOUS_RESEARCH_SETTINGS") or "").strip(),
            default_markets=settings.markets,
            default_asset_classes=tuple(AssetClass),
            default_strategies=tuple(registered_research_strategies),
        )
        autonomous_research = AutonomousResearchService(
            scanner, candidate_research, candidate_assessment,
            store.autonomous_research, registry, autonomous_settings,
        )
    signal_generation = SignalGenerationService(
        market_data,
        store.autonomous_research,
        store.research_runs,
        store.research_runs,
        store.signal_generations,
        instruments,
        registry,
        registered_research_strategies,
        research_sessions,
        settings=parse_signal_generation_settings(
            env.get("SIGNAL_GENERATION_SETTINGS")),
    )
    trade_proposals = TradeProposalService(
        autonomous_repository=store.autonomous_research,
        research_repository=store.research_runs,
        opportunity_repository=store.research_runs,
        signal_generation_repository=store.signal_generations,
        signal_repository=store.signals,
        proposal_repository=store.trade_proposals,
        portfolio=portfolio,
        risk_engine=risk_engine,
        sizing_limits=sizing_limits,
        instruments=instruments,
        markets=registry,
        order_manager=order_manager,
        market_stats=market_stats,
        sector_of=sector_of,
        settings=TradeProposalSettings(
            max_opportunity_age=signal_generation.settings.max_opportunity_age,
            max_market_data_age=signal_generation.settings.max_market_data_age,
        ),
    )
    health.register_check("market_data_provider", lambda: CheckResult(
        market_data.breaker_state != "OPEN",
        f"{market_data.name} circuit {market_data.breaker_state.lower()}"))
    quotes = quote_source(market_data, instruments, logger=logger)
    paper = TradingService(
        mode=TradingMode.PAPER, risk_engine=risk_engine,
        order_manager=order_manager, portfolio=portfolio, instruments=instruments,
        quotes=quotes,
        market_stats=market_stats, sector_of=sector_of, store=store, gate=gate,
        strategy_approval=strategies.check_live, provenance_source=strategies.version_info,
        sizing_limits=sizing_limits)
    max_execution_data_age = timedelta(
        seconds=settings.max_market_data_age_seconds)
    paper_proposal_execution = PaperProposalExecutionService(
        store=store,
        trading=paper,
        instruments=instruments,
        markets=registry,
        quotes=quotes,
        update_price=broker.update_price,
        strategies=registered_research_strategies,
        max_age=max_execution_data_age,
    )
    paper_position_manager = PaperPositionManager(
        store=store,
        trading=paper,
        instruments=instruments,
        markets=registry,
        quotes=quotes,
        update_price=broker.update_price,
        max_age=max_execution_data_age,
    )
    research_pipeline = None
    market_intelligence = None
    if research_analyst is not None:
        research_evidence_producers = []
        if fundamental_observation_provider is not None:
            research_evidence_producers.append(FundamentalEvidenceProducer(
                fundamental_observation_provider))
        if sector_observation_provider is not None:
            research_evidence_producers.append(SectorEvidenceProducer(
                sector_observation_provider))
        news_evidence_producer = None
        if raw_news_provider is not None:
            news_evidence_producer = NewsEvidenceProducer(
                raw_news_provider,
                limit=5,
            )
        research_pipeline = StrategyResearchPipeline(
            market_data,
            research_analyst,
            registered_research_strategies,
            trading_service=paper,
            news_evidence_producer=news_evidence_producer,
            research_evidence_producers=tuple(research_evidence_producers),
        )
        if news_evidence_producer is not None:
            market_intelligence = MarketIntelligenceOrchestrator(research_pipeline)
    recovery = RecoveryManager(store=store, order_manager=order_manager, portfolio=portfolio,
                               broker=broker, gate=gate)
    # connects the broker, restores orders, and halts entries on any discrepancy
    recovery.recover()

    live_strategies = tuple(s.strip() for s in (
        env.get("LIVE_STRATEGIES") or "").split(",") if s.strip())
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
                      kill_switch=kill_switch, monitor=monitor, market_data=market_data,
                      instruments=instruments, research_pipeline=research_pipeline,
                      research_sessions=research_sessions or {},
                      market_intelligence=market_intelligence,
                      scanner=scanner,
                      candidate_research=candidate_research,
                      candidate_assessment=candidate_assessment,
                      autonomous_research=autonomous_research,
                      signal_generation=signal_generation,
                      trade_proposals=trade_proposals,
                      paper_proposal_execution=paper_proposal_execution,
                      paper_position_manager=paper_position_manager)


def create_app_from_env() -> FastAPI:
    """uvicorn stockmarket.api.bootstrap:create_app_from_env --factory"""
    settings = load_settings()
    return create_app(build_context(settings))
