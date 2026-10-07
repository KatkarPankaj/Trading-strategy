"""FastAPI application. Handlers only authenticate, validate and delegate; business rules live in core."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..core.audit_trail import AuditContext, reconstruct
from ..core.executors import LIVE_CONFIRMATION_PHRASE, LiveTradingRefused, TradingMode
from ..core.kill_switch import KillSwitchError
from ..core.markets import UnknownMarket
from ..core.observability import HealthMonitor
from ..core.order_management import IdempotencyConflict, InvalidOrderTransition, OrderManagerError, UnknownOrder
from ..core.market_session import MarketSession
from ..core.models import Instrument
from ..core.persistence import Store, to_json
from ..core.recovery import RecoveryError
from ..core.security import Secret
from ..core.settings import AppSettings
from ..core.strategy_pipeline import StrategyResearchPipeline
from ..core.trading_service import OrderTicket, TradingService, UnknownInstrument, summarize_trades
from .schemas import KillResetBody, KillTriggerBody, OrderBody, ResearchRunBody, ResumeBody


@dataclass(slots=True)
class ApiContext:
    settings: AppSettings
    paper: TradingService
    live: TradingService | None
    store: Store
    health: HealthMonitor
    api_token: Secret | None = None
    gate: Any = None
    recovery: Any = None
    markets: Any = None
    readiness: Any = None
    kill_switch: Any = None
    monitor: Any = None
    market_data: Any = None
    instruments: Mapping[str, Instrument] = field(default_factory=dict)
    research_pipeline: StrategyResearchPipeline | None = None
    research_sessions: Mapping[str, MarketSession] = field(default_factory=dict)

    @property
    def primary(self) -> TradingService:
        return self.live or self.paper

    @property
    def mode(self) -> str:
        return TradingMode.LIVE.value if self.live else TradingMode.PAPER.value


def _plain(value: Any) -> Any:
    return json.loads(to_json(value))


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(title="Trading Platform API", version="0.1.0")
    if ctx.market_data is not None:
        app.add_event_handler("shutdown", ctx.market_data.close)

    @app.middleware("http")
    async def show_mode(request: Request, call_next):  # the trading mode is never hidden
        response = await call_next(request)
        response.headers["X-Trading-Mode"] = ctx.mode
        return response

    def auth(authorization: str | None = Header(default=None)) -> None:
        if ctx.api_token is None:
            raise HTTPException(
                503, "API token is not configured; protected endpoints are disabled")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.encode(), ctx.api_token.reveal().encode()):
            raise HTTPException(401, "invalid or missing bearer token", headers={
                                "WWW-Authenticate": "Bearer"})

    def live_guard(x_live_confirm: str | None = Header(default=None)) -> TradingService:
        if ctx.live is None or ctx.settings.execution.mode is not TradingMode.LIVE:
            raise HTTPException(
                403, "live trading is not enabled on this server")
        if x_live_confirm != LIVE_CONFIRMATION_PHRASE:
            raise HTTPException(
                403, "missing or incorrect X-Live-Confirm header")
        return ctx.live

    def submit(service: TradingService, body: OrderBody, actor: str) -> dict[str, Any]:
        fields = body.model_dump()
        audit = fields.pop("audit")
        if audit is not None:
            audit["ai_analysis_ids"] = tuple(audit["ai_analysis_ids"])
            fields["audit"] = AuditContext(**audit)
        ticket = OrderTicket(**fields)
        try:
            result = service.submit(ticket, actor=actor)
        except UnknownInstrument as exc:
            raise HTTPException(404, f"unknown instrument {exc}") from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LiveTradingRefused as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"mode": service.mode.value, "duplicate": result.duplicate,
                "order": _plain(result.order), "risk_decision": _plain(result.risk)}

    @app.get("/metrics", dependencies=[Depends(auth)], response_class=PlainTextResponse)
    def metrics() -> str:
        report = ctx.health.report()
        p = ctx.primary.portfolio
        rows = [
            ("trading_up", "", 1), ("trading_health_ok",
                                    "", int(report["status"] == "OK")),
            ("trading_mode_live", "", int(ctx.live is not None)),
            ("trading_halted", "", int(bool(ctx.gate and ctx.gate.halted))),
            ("trading_kill_switch_active", "", int(
                bool(ctx.kill_switch and ctx.kill_switch.active))),
            ("trading_equity", "", p.equity), ("trading_drawdown",
                                               "", p.drawdown), ("trading_daily_pnl", "", p.daily_pnl),
        ]
        if report["market_data_age_seconds"] is not None:
            rows.append(("trading_market_data_age_seconds",
                        "", report["market_data_age_seconds"]))
        rows += [("trading_errors_total", f'{{component="{c}"}}', n)
                 for c, n in report["error_counts"].items()]
        for service in (ctx.paper, ctx.live):
            if service is not None:
                rows.append(("trading_open_orders", f'{{mode="{service.mode.value}"}}', len(
                    service.order_manager.open_orders())))
        return "".join(f"{name}{labels} {value}\n" for name, labels, value in rows)

    @app.get("/health")
    def health() -> JSONResponse:
        report = ctx.health.report()
        report["trading_mode"] = ctx.mode
        return JSONResponse(report, status_code=200 if report["status"] != "DOWN" else 503)

    @app.get("/markets", dependencies=[Depends(auth)])
    def markets() -> dict[str, Any]:
        rows = ctx.store.instruments.list()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["market"]] = counts.get(r["market"], 0) + 1
        now = datetime.now(timezone.utc)
        out = []
        for m in ctx.settings.markets:
            row: dict[str, Any] = {"market": m,
                                   "instruments": counts.get(m, 0)}
            if ctx.markets is not None:
                definition = ctx.markets.get(m)
                row.update(phase=definition.phase(now).value, currency=definition.currency,
                           timezone=definition.timezone, calendar_covered=definition.is_covered(now.date()))
            out.append(row)
        return {"trading_mode": ctx.mode, "markets": out}

    @app.get("/instruments", dependencies=[Depends(auth)])
    def instruments(market: str | None = Query(default=None, max_length=16)) -> list[dict[str, Any]]:
        return _plain(ctx.store.instruments.list(market.upper() if market else None))

    @app.get("/signals", dependencies=[Depends(auth)])
    def signals(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.signals.recent(limit))

    @app.get("/positions", dependencies=[Depends(auth)])
    def positions() -> dict[str, Any]:
        return {"trading_mode": ctx.mode,
                "positions": _plain(list(ctx.primary.portfolio.positions().values()))}

    @app.get("/orders", dependencies=[Depends(auth)])
    def orders(open_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for service in (ctx.paper, ctx.live):
            if service is None:
                continue
            manager = service.order_manager
            for o in (manager.open_orders() if open_only else manager.orders()):
                out.append({"mode": service.mode.value, **_plain(o)})
        return out

    @app.get("/trades", dependencies=[Depends(auth)])
    def trades(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.trades.recent(limit))

    @app.get("/portfolio", dependencies=[Depends(auth)])
    def portfolio() -> dict[str, Any]:
        p = ctx.primary.portfolio
        return _plain({
            "trading_mode": ctx.mode, "base_currency": p.base_currency, "equity": p.equity,
            "cash": p.cash, "cash_base": p.cash_base, "realized_pnl": p.realized_pnl,
            "unrealized_pnl": p.unrealized_pnl, "fees": p.fees, "slippage": p.slippage,
            "gross_exposure": p.gross_exposure, "net_exposure": p.net_exposure,
            "sector_exposure": p.sector_exposure(), "currency_exposure": p.currency_exposure(),
            "peak_equity": p.peak_equity, "drawdown": p.drawdown,
            "daily_pnl": p.daily_pnl, "monthly_pnl": p.monthly_pnl})

    @app.get("/performance", dependencies=[Depends(auth)])
    def performance() -> dict[str, Any]:
        return _plain({"trading_mode": ctx.mode, "trades": summarize_trades(ctx.store.trades.recent(1000)),
                       "latest_pnl_snapshot": ctx.store.pnl.latest()})

    @app.post("/research", dependencies=[Depends(auth)])
    def run_research(body: ResearchRunBody) -> dict[str, Any]:
        if ctx.research_pipeline is None:
            raise HTTPException(503, "strategy research pipeline is not configured")
        instrument = ctx.instruments.get(body.instrument_id)
        if instrument is None:
            raise HTTPException(404, "unknown instrument")
        session = ctx.research_sessions.get(instrument.market)
        if session is None:
            raise HTTPException(
                503, f"market session is not configured for {instrument.market}")
        as_of = body.as_of or datetime.now(timezone.utc)
        if ctx.markets is not None:
            try:
                market = ctx.markets.get(instrument.market)
            except UnknownMarket as exc:
                raise HTTPException(
                    503, f"market calendar is unavailable for {instrument.market}") from exc
            market_date = as_of.astimezone(ZoneInfo(market.timezone)).date()
            if not market.is_covered(market_date):
                raise HTTPException(
                    503, f"market calendar is not covered for {instrument.market} "
                    f"in {market_date.year}")
            if not market.calendar.is_trading_day(market_date):
                raise HTTPException(
                    422, f"research date is not a trading day for {instrument.market}")
        try:
            evidence = tuple(
                item.to_domain(instrument.instrument_id) for item in body.evidence)
            result = ctx.research_pipeline.run(
                instrument, session, as_of=as_of, research_evidence=evidence)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "trading_mode": ctx.mode,
            "status": result.status.value,
            "reason": result.reason,
            "instrument_id": instrument.instrument_id,
            "as_of": as_of.isoformat(),
            "regime": _plain(result.regime) if result.regime is not None else None,
            "selection": _plain(result.selection) if result.selection is not None else None,
            "signal": _plain(result.strategy_signal)
            if result.strategy_signal is not None else None,
            "decision": _plain(result.decision) if result.decision is not None else None,
            "research_evidence": [
                {
                    "instrument_id": item.instrument_id,
                    "component": item.component,
                    "score": item.score,
                    "observed_at": item.observed_at.isoformat(),
                    "source": item.source,
                    "history_trades": item.history_trades,
                    "max_age_seconds": item.max_age.total_seconds()
                    if item.max_age is not None else None,
                }
                for item in result.research_evidence
            ],
            "research_warnings": list(result.research_warnings),
            "bar_count": len(result.bars) if result.bars is not None else 0,
            "latest_bar_at": result.bars.index[-1].isoformat()
            if result.bars is not None and not result.bars.empty else None,
        }

    @app.get("/orders/{client_order_id}/audit", dependencies=[Depends(auth)])
    def order_audit(client_order_id: str) -> dict[str, Any]:
        trail = reconstruct(ctx.store, client_order_id)
        if trail is None:
            raise HTTPException(404, "unknown order")
        return _plain(trail)

    @app.get("/executions", dependencies=[Depends(auth)])
    def executions(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.execution_records.recent(limit))

    @app.get("/pnl", dependencies=[Depends(auth)])
    def pnl(limit: int = Query(default=500, ge=1, le=5000)) -> list[dict[str, Any]]:
        return _plain(list(reversed(ctx.store.pnl.recent(limit))))

    @app.get("/strategy-decisions", dependencies=[Depends(auth)])
    def strategy_decisions(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.strategy_decisions.recent(limit))

    @app.get("/news", dependencies=[Depends(auth)])
    def news(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.news.recent_events(limit))

    @app.get("/ai-analyses", dependencies=[Depends(auth)])
    def ai_analyses(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _plain(ctx.store.news.recent_analyses(limit))

    @app.get("/risk", dependencies=[Depends(auth)])
    def risk(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        p = ctx.primary.portfolio
        return _plain({
            "trading_mode": ctx.mode,
            "limits": {k: getattr(ctx.settings.risk, k) for k in ctx.settings.risk.__slots__},
            "disabled_controls": ctx.primary.disabled_controls,
            "current": {"drawdown": p.drawdown, "daily_pnl": p.daily_pnl, "equity": p.equity,
                        "gross_exposure": p.gross_exposure},
            "recent_decisions": ctx.store.risk_decisions.recent(limit)})

    @app.get("/audit", dependencies=[Depends(auth)])
    def audit(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        entries = ctx.store.audit.entries()
        problems = ctx.store.audit.verify_chain()
        return _plain({"chain_intact": not problems, "problems": problems,
                       "entries": list(reversed(entries[-limit:]))})

    @app.get("/recovery", dependencies=[Depends(auth)])
    def recovery_status() -> dict[str, Any]:
        return {"trading_mode": ctx.mode, "halted": bool(ctx.gate and ctx.gate.halted),
                "reasons": ctx.gate.reasons() if ctx.gate else {}}

    @app.post("/recovery/resume", dependencies=[Depends(auth)])
    def recovery_resume(body: ResumeBody) -> dict[str, Any]:
        if ctx.recovery is None:
            raise HTTPException(404, "recovery is not configured")
        try:
            report = ctx.recovery.resume(
                body.operator, body.note, body.acknowledged)
        except RecoveryError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _plain({"halted": ctx.gate.halted, "discrepancies": report.discrepancies})

    @app.post("/paper/orders", dependencies=[Depends(auth)])
    def paper_order(body: OrderBody) -> dict[str, Any]:
        return submit(ctx.paper, body, "api:paper")

    @app.get("/kill-switch", dependencies=[Depends(auth)])
    def kill_switch_state() -> dict[str, Any]:
        ks = ctx.kill_switch
        if ks is None:
            raise HTTPException(404, "kill switch is not configured")
        return {"trading_mode": ctx.mode, "active": ks.active, "reasons": ctx.gate.reasons(),
                "breaching_conditions": [{"code": c, "detail": d} for c, d in (ctx.monitor.evaluate() if ctx.monitor else [])]}

    @app.post("/kill-switch/trigger", dependencies=[Depends(auth)])
    def kill_switch_trigger(body: KillTriggerBody) -> dict[str, Any]:
        if ctx.kill_switch is None:
            raise HTTPException(404, "kill switch is not configured")
        event = ctx.kill_switch.trigger("MANUAL", body.reason, source="manual", actor=f"api:{body.operator}",
                                        cancel_open_orders=body.cancel_open_orders)
        return _plain(event)

    @app.post("/kill-switch/reset", dependencies=[Depends(auth)])
    def kill_switch_reset(body: KillResetBody) -> dict[str, Any]:
        if ctx.kill_switch is None:
            raise HTTPException(404, "kill switch is not configured")
        try:
            ctx.kill_switch.reset(body.operator, body.note,
                                  override=body.override)
        except KillSwitchError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"active": ctx.kill_switch.active}

    @app.get("/live/readiness", dependencies=[Depends(auth)])
    def live_readiness() -> dict[str, Any]:
        if ctx.readiness is None:
            raise HTTPException(404, "readiness checking is not configured")
        report = ctx.readiness.run()
        return _plain({"trading_mode": ctx.mode, "ready": report.ready, "checked_at": report.checked_at,
                       "checks": report.checks})

    @app.post("/live/orders", dependencies=[Depends(auth)])
    def live_order(body: OrderBody, service: TradingService = Depends(live_guard)) -> dict[str, Any]:
        return submit(service, body, "api:live")

    @app.post("/orders/{client_order_id}/cancel", dependencies=[Depends(auth)])
    def cancel(client_order_id: str) -> dict[str, Any]:
        for service in (ctx.paper, ctx.live):
            if service is None:
                continue
            try:
                service.order_manager.get(client_order_id)
            except UnknownOrder:
                continue
            try:
                order = service.cancel(
                    client_order_id, actor=f"api:{service.mode.value.lower()}")
            except InvalidOrderTransition as exc:
                raise HTTPException(409, str(exc)) from exc
            except OrderManagerError as exc:
                raise HTTPException(502, str(exc)) from exc
            except Exception as exc:  # broker-side failure; details stay in logs, not the response
                raise HTTPException(
                    502, f"cancel failed: {type(exc).__name__}") from exc
            return {"mode": service.mode.value, "order": _plain(order)}
        raise HTTPException(404, "unknown order")

    return app
