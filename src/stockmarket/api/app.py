"""FastAPI application. Handlers only authenticate, validate and delegate; business rules live in core."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..core.audit_trail import AuditContext, reconstruct
from ..core.candidate_research import CandidateResearchService
from ..core.ai.candidate_assessment import (
    CandidateAssessmentError,
    CandidateAssessmentService,
)
from ..core.executors import LIVE_CONFIRMATION_PHRASE, LiveTradingRefused, TradingMode
from ..core.kill_switch import KillSwitchError
from ..core.markets import UnknownMarket
from ..core.observability import HealthMonitor
from ..core.order_management import IdempotencyConflict, InvalidOrderTransition, OrderManagerError, UnknownOrder
from ..core.market_session import MarketSession
from ..core.market_intelligence import (
    MarketIntelligenceOrchestrator,
    ProposalSubmissionContext,
)
from ..core.models import Instrument
from ..core.persistence import Store, to_json
from ..core.recovery import RecoveryError
from ..core.scanner import MarketScanner, ScanPersistenceError, UnknownUniverse
from ..core.security import Secret
from ..core.settings import AppSettings
from ..core.strategy_pipeline import StrategyResearchPipeline
from ..core.trading_service import (
    AutomaticSizingRejected, OrderTicket, TradingService, UnknownInstrument,
    summarize_trades,
)
from .schemas import (
    CandidateResearchRunBody,
    KillResetBody,
    KillTriggerBody,
    OpportunityRunBody,
    OrderBody,
    ProposalSubmitBody,
    ResearchRunBody,
    ResumeBody,
    ScannerRunBody,
)


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
    market_intelligence: MarketIntelligenceOrchestrator | None = None
    scanner: MarketScanner | None = None
    candidate_research: CandidateResearchService | None = None
    candidate_assessment: CandidateAssessmentService | None = None

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
    proposal_contexts: OrderedDict[str, ProposalSubmissionContext] = OrderedDict()
    proposal_contexts_lock = RLock()
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

    @app.get("/universes", dependencies=[Depends(auth)])
    def universes() -> list[dict[str, Any]]:
        if ctx.scanner is None:
            raise HTTPException(503, "market scanner is not configured")
        output = []
        for definition in ctx.scanner.list_universes():
            _, instruments = ctx.scanner.get_universe(definition.universe_id)
            output.append({
                "universe": _plain(definition),
                "instrument_count": len(instruments),
            })
        return output

    @app.get("/universes/{universe_id}", dependencies=[Depends(auth)])
    def universe(universe_id: str) -> dict[str, Any]:
        if ctx.scanner is None:
            raise HTTPException(503, "market scanner is not configured")
        try:
            definition, instruments = ctx.scanner.get_universe(universe_id)
        except UnknownUniverse as exc:
            raise HTTPException(404, f"unknown universe {universe_id!r}") from exc
        return {"universe": _plain(definition), "instrument_count": len(instruments)}

    @app.post("/scanner/scan", dependencies=[Depends(auth)])
    def run_scan(body: ScannerRunBody) -> dict[str, Any]:
        if ctx.scanner is None:
            raise HTTPException(503, "market scanner is not configured")
        try:
            return _plain(ctx.scanner.scan(
                body.universe_id, body.mode, top_n=body.top_n, as_of=body.as_of,
            ))
        except UnknownUniverse as exc:
            raise HTTPException(404, f"unknown universe {body.universe_id!r}") from exc
        except ScanPersistenceError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/scanner/runs/{scan_id}", dependencies=[Depends(auth)])
    def scanner_run(scan_id: str) -> dict[str, Any]:
        run = ctx.store.scanner_runs.get(scan_id)
        if run is None:
            raise HTTPException(404, f"unknown scan run {scan_id!r}")
        return _plain(run)

    @app.get("/scanner/candidates", dependencies=[Depends(auth)])
    def scanner_candidates(
        scan_id: str = Query(min_length=1, max_length=64),
        accepted_only: bool = False,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        if ctx.store.scanner_runs.get(scan_id) is None:
            raise HTTPException(404, f"unknown scan run {scan_id!r}")
        return _plain(ctx.store.scanner_runs.candidates(
            scan_id, accepted_only=accepted_only, limit=limit, offset=offset,
        ))

    @app.post("/research/candidates", dependencies=[Depends(auth)])
    def run_candidate_research(body: CandidateResearchRunBody) -> dict[str, Any]:
        if ctx.candidate_research is None:
            raise HTTPException(503, "candidate research is not configured")
        try:
            run, snapshots = ctx.candidate_research.run_scan(
                body.scan_id, as_of=body.as_of, limit=body.limit)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "research_only": True,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
            "run": _plain(run),
            "snapshots": [_plain(snapshot) for snapshot in snapshots],
        }

    @app.get("/research/runs/{run_id}", dependencies=[Depends(auth)])
    def get_candidate_research_run(run_id: str) -> dict[str, Any]:
        if ctx.candidate_research is None:
            raise HTTPException(503, "candidate research is not configured")
        row = ctx.store.research_runs.get_run(run_id)
        if row is None:
            raise HTTPException(404, "unknown research run")
        return {
            "run": row["payload"],
            "snapshots": [
                item["payload"]
                for item in ctx.store.research_runs.snapshots(run_id)
            ],
        }

    @app.get("/research/snapshots/{snapshot_id}", dependencies=[Depends(auth)])
    def get_candidate_research_snapshot(snapshot_id: str) -> dict[str, Any]:
        if ctx.candidate_research is None:
            raise HTTPException(503, "candidate research is not configured")
        row = ctx.store.research_runs.get_snapshot(snapshot_id)
        if row is None:
            raise HTTPException(404, "unknown research snapshot")
        return {
            "snapshot": row["payload"],
            "evidence": [
                item["payload"]
                for item in ctx.store.research_runs.evidence(snapshot_id)
            ],
        }

    @app.post("/research/assessments/{snapshot_id}", dependencies=[Depends(auth)])
    def assess_candidate_snapshot(snapshot_id: str) -> dict[str, Any]:
        if ctx.candidate_assessment is None:
            raise HTTPException(503, "AI candidate assessment is not configured")
        try:
            opportunity = ctx.candidate_assessment.assess(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CandidateAssessmentError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "research_only": True,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
            "opportunity": _plain(opportunity),
        }

    @app.get("/research/assessments/{assessment_id}", dependencies=[Depends(auth)])
    def get_candidate_assessment(assessment_id: str) -> dict[str, Any]:
        if ctx.candidate_assessment is None:
            raise HTTPException(503, "AI candidate assessment is not configured")
        assessment = ctx.candidate_assessment.get_assessment(assessment_id)
        if assessment is None:
            raise HTTPException(404, "unknown AI research assessment")
        return assessment

    @app.get("/research/opportunities", dependencies=[Depends(auth)])
    def list_research_opportunities(
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if ctx.candidate_assessment is None:
            raise HTTPException(503, "AI candidate assessment is not configured")
        return {
            "research_only": True,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
            "opportunities": ctx.candidate_assessment.opportunities(
                limit=limit, offset=offset),
        }

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

    @app.post("/intelligence/opportunities", dependencies=[Depends(auth)])
    def rank_opportunities(body: OpportunityRunBody) -> dict[str, Any]:
        if ctx.market_intelligence is None:
            raise HTTPException(503, "market intelligence service is not configured")
        if ctx.mode != TradingMode.PAPER.value:
            raise HTTPException(
                403, "market-intelligence proposals are available only in PAPER mode")
        now = datetime.now(timezone.utc)
        if body.as_of > now:
            raise HTTPException(422, "as_of must not be in the future")

        candidates: list[tuple[Instrument, MarketSession]] = []
        for instrument_id in body.instrument_ids:
            instrument = ctx.instruments.get(instrument_id)
            if instrument is None:
                raise HTTPException(404, f"unknown instrument {instrument_id!r}")
            session = ctx.research_sessions.get(instrument.market)
            if session is None:
                raise HTTPException(
                    503, f"market session is not configured for {instrument.market}")
            if ctx.markets is not None:
                try:
                    market = ctx.markets.get(instrument.market)
                except UnknownMarket as exc:
                    raise HTTPException(
                        503, f"market calendar is unavailable for {instrument.market}") from exc
                market_date = body.as_of.astimezone(
                    ZoneInfo(market.timezone)).date()
                if not market.is_covered(market_date):
                    raise HTTPException(
                        503, f"market calendar is not covered for {instrument.market} "
                        f"in {market_date.year}")
                if not market.calendar.is_trading_day(market_date):
                    raise HTTPException(
                        422, f"research date is not a trading day for {instrument.market}")
            candidates.append((instrument, session))

        try:
            result = ctx.market_intelligence.run(
                candidates, as_of=body.as_of)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        with proposal_contexts_lock:
            for submission_context in result.submission_contexts:
                proposal_id = submission_context.proposal.proposal_id
                proposal_contexts.pop(proposal_id, None)
                proposal_contexts[proposal_id] = submission_context
            while len(proposal_contexts) > 1000:
                proposal_contexts.popitem(last=False)
        return {
            "trading_mode": TradingMode.PAPER.value,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
            "as_of": result.as_of.isoformat(),
            "generated_at": result.generated_at.isoformat(),
            "ranking_method": (
                "deterministic aggregate confidence descending; "
                "absolute aggregate score descending; instrument_id ascending"),
            "proposals": [_plain(item) for item in result.proposals],
            "assessments": [_plain(item) for item in result.assessments],
        }

    @app.post(
        "/intelligence/proposals/{proposal_id}/submit",
        dependencies=[Depends(auth)],
    )
    def submit_proposal(proposal_id: str, body: ProposalSubmitBody) -> dict[str, Any]:
        if ctx.mode != TradingMode.PAPER.value \
                or ctx.paper.mode is not TradingMode.PAPER:
            raise HTTPException(403, "proposal submission is restricted to PAPER mode")
        if ctx.research_pipeline is None or ctx.market_intelligence is None:
            raise HTTPException(503, "paper proposal submission is not configured")

        def prior_submission(record: Mapping[str, Any]) -> dict[str, Any]:
            if str(record["state"]).startswith("ORDER_"):
                try:
                    order = ctx.paper.order_manager.get(str(record["client_order_id"]))
                except UnknownOrder as exc:
                    raise HTTPException(
                        409, "proposal submission is persisted but order recovery is incomplete") from exc
                return {
                    "trading_mode": TradingMode.PAPER.value,
                    "execution": "PAPER_ORDER_ALREADY_CREATED",
                    "duplicate": True,
                    "proposal_id": proposal_id,
                    "operator": body.operator,
                    "order": _plain(order),
                    "risk_decision": None,
                }
            if record["state"] == "REJECTED_BEFORE_ORDER":
                raise HTTPException(
                    409, f"proposal was rejected before order creation: {record['error']}")
            raise HTTPException(
                409, "proposal submission is already claimed; reconcile before retrying")

        prior = ctx.store.proposal_submissions.get(proposal_id)
        if prior is not None:
            return prior_submission(prior)

        with proposal_contexts_lock:
            submission_context = proposal_contexts.get(proposal_id)
            if submission_context is not None:
                proposal_contexts.move_to_end(proposal_id)
        if submission_context is None:
            raise HTTPException(404, "unknown or expired proposal")

        now = datetime.now(timezone.utc)
        maximum_age = timedelta(
            seconds=ctx.settings.max_market_data_age_seconds)
        proposal = submission_context.proposal
        if proposal.as_of > now or proposal.generated_at > now \
                or now - proposal.as_of > maximum_age \
                or now - proposal.generated_at > maximum_age:
            raise HTTPException(409, "proposal is stale and must be regenerated")

        client_order_id = "proposal-" + hashlib.sha256(
            proposal_id.encode("utf-8")).hexdigest()[:48]
        claimed = ctx.store.proposal_submissions.begin(
            proposal_id=proposal_id,
            client_order_id=client_order_id,
            operator=body.operator,
            sizing_mode=body.sizing_mode,
            quantity=body.quantity,
            proposal_as_of=proposal.as_of,
            generated_at=proposal.generated_at,
            proposal_payload=_plain(proposal),
        )
        if not claimed:
            prior = ctx.store.proposal_submissions.get(proposal_id)
            if prior is None:
                raise HTTPException(503, "proposal claim disappeared during submission")
            return prior_submission(prior)
        try:
            result = ctx.research_pipeline.submit_decision(
                submission_context.pipeline_result,
                body.quantity,
                sizing_mode=body.sizing_mode,
                actor=body.operator,
                client_order_id=client_order_id,
                proposal_id=proposal_id,
            )
        except AutomaticSizingRejected as exc:
            ctx.store.proposal_submissions.finish(
                proposal_id,
                state="REJECTED_BEFORE_ORDER",
                quantity=None,
                error=str(exc),
            )
            raise HTTPException(422, str(exc)) from exc
        except (IdempotencyConflict, InvalidOrderTransition) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            ctx.store.proposal_submissions.finish(
                proposal_id,
                state="REJECTED_BEFORE_ORDER",
                quantity=body.quantity,
                error=str(exc),
            )
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        ctx.store.proposal_submissions.finish(
            proposal_id,
            state=f"ORDER_{result.order.status.value}",
            quantity=result.order.quantity,
            error=result.order.error,
        )
        return {
            "trading_mode": TradingMode.PAPER.value,
            "execution": "PAPER_ORDER_CREATED",
            "duplicate": result.duplicate,
            "proposal_id": proposal_id,
            "operator": body.operator,
            "order": _plain(result.order),
            "risk_decision": _plain(result.risk),
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
