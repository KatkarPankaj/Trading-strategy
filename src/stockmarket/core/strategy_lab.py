"""Constrained AI hypotheses evaluated by trusted, causal strategy/backtest code."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from math import isfinite
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from .ai.candidate_assessment import CandidateAssessmentService
from .ai.schemas import StrategyProposalSchema
from .backtesting.engine import BacktestConfig, BacktestEngine, BacktestSignal
from .backtesting.execution_model import ExecutionModel
from .backtesting.walk_forward import make_windows, session_dates
from .data.provider import MarketDataProvider, interval_delta
from .data.quality import completed_intraday_bars, validate_bars
from .market_session import MarketSession
from .models import Instrument, OrderSide, TradeIntent
from .persistence import Store
from .persistence.repositories import to_json, ts
from .sizing import SizingLimits
from .strategies import OrbVwapStrategy
from .strategies.orb_vwap import OrbVwapConfig
from .strategies.base import strategy_parameters_hash


class StrategyLabError(ValueError):
    """A proposal or evaluation request is unsafe or lacks required evidence."""


class StrategyLabService:
    def __init__(
        self, store: Store, assessment: CandidateAssessmentService,
        market_data: MarketDataProvider, instruments: Mapping[str, Instrument],
        sessions: Mapping[str, MarketSession], sizing: SizingLimits,
        *, clock: Callable[[], datetime],
        flatten_minutes: int = 10,
    ) -> None:
        self.store, self.assessment, self.market_data = store, assessment, market_data
        self.instruments, self.sessions, self.sizing = instruments, sessions, sizing
        self.clock = clock
        if isinstance(flatten_minutes, bool) or not isinstance(flatten_minutes, int) \
                or not 1 <= flatten_minutes <= 120:
            raise ValueError("flatten_minutes must be between 1 and 120")
        self.flatten_minutes = flatten_minutes

    def propose(self, snapshot_id: str) -> dict[str, Any]:
        context = self.assessment.snapshot_context(snapshot_id)
        request = json.dumps(context.as_mapping(), sort_keys=True, separators=(",", ":"))
        if len(request) > self.assessment.analyst.max_input_chars:
            raise StrategyLabError("proposal context exceeds the configured AI input limit")
        # The same validated provenance/temporal constraints as production assessment.
        result = self.assessment.analyst.analyze(
            "strategy_proposal",
            request)
        if not result.ok or not isinstance(result.output, StrategyProposalSchema):
            raise StrategyLabError(result.error or "invalid strategy proposal output")
        spec = result.output
        if result.created_at < context.as_of or result.created_at > self.clock():
            raise StrategyLabError("AI proposal timestamp is invalid")
        if spec.market != context.market:
            raise StrategyLabError("AI proposal market does not match its evidence")
        instrument = self.instruments[context.instrument_id]
        if spec.parameters.allow_short and instrument.shortable is not True:
            raise StrategyLabError("AI proposal requires unverified short-selling eligibility")
        strategy = self._strategy(spec)
        identity = hashlib.sha256(to_json({
            "snapshot_id": snapshot_id, "specification": spec.model_dump(),
            "template_version": strategy.version,
        }).encode()).hexdigest()
        payload = {
            "proposal_id": identity, "strategy_version_id": f"orb_vwap:{identity[:24]}",
            "snapshot_id": snapshot_id, "instrument_id": context.instrument_id,
            "template_version": strategy.version, "specification": spec.model_dump(),
            "parameters_hash": strategy_parameters_hash(strategy),
            "validation_status": "NO_TRADE" if spec.outcome == "NO_TRADE" else "AWAITING_EVALUATION",
            "executable": False, "provider": result.provider,
            "prompt_hash": result.prompt_hash, "response_hash": result.response_hash,
            "research_context": context.as_mapping(),
        }
        # Same specification for the same evidence is a retry, not a new version.
        existing = self.store.strategy_lab.get(identity)
        if existing is not None:
            return existing
        self.store.strategy_lab.save_proposal(payload, self.clock())
        return payload

    @staticmethod
    def _strategy(spec: StrategyProposalSchema) -> OrbVwapStrategy:
        return OrbVwapStrategy(OrbVwapConfig(
            interval=spec.timeframe, **spec.parameters.model_dump(),
            max_bar_age=interval_delta(spec.timeframe)))

    @staticmethod
    def _validate_costs(commission_rate: float, slippage_bps: float) -> None:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not isfinite(value) or not 0 < value <= ceiling
               for value, ceiling in ((commission_rate, .01), (slippage_bps, 100))):
            raise StrategyLabError("invalid evaluation cost assumptions")

    def _evaluation_policy(
        self, proposal: Mapping[str, Any], spec: StrategyProposalSchema,
        start: datetime, end: datetime, commission_rate: float, slippage_bps: float,
    ) -> dict[str, Any]:
        instrument = self.instruments[proposal["instrument_id"]]
        market = self.assessment.markets.get(instrument.market)
        session = self.sessions.get(instrument.market)
        calendar = asdict(market.calendar)
        calendar["early_closes"] = {day.isoformat(): close.isoformat()
                                    for day, close in market.calendar.early_closes.items()}
        return json.loads(to_json({
            "proposal_id": proposal["proposal_id"], "start": ts(start), "end": ts(end),
            "parameters_hash": proposal["parameters_hash"],
            "costs": {"commission_rate": commission_rate, "slippage_bps": slippage_bps,
                      "spread_bps": 2, "participation_rate": .01, "latency_bars": 1},
            "sizing": self.sizing, "calendar": calendar,
            "session": session, "instrument": instrument,
            "bar_interval": spec.timeframe, "flatten_minutes": self.flatten_minutes,
            "evaluation_policy_version": "session-holdout-v1",
        }))

    def register_holdout(
        self, proposal_id: str, *, start: datetime, end: datetime,
        commission_rate: float = .001, slippage_bps: float = 5,
    ) -> dict[str, Any]:
        """Freeze one future observation window before requesting any holdout data."""
        now = self.clock()
        ts(start), ts(end), ts(now)
        self._validate_costs(commission_rate, slippage_bps)
        if start >= end or end - start > timedelta(days=180):
            raise StrategyLabError("holdout window must span at most 180 days")
        proposal = self.store.strategy_lab.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        spec = StrategyProposalSchema.model_validate(proposal["specification"])
        if spec.outcome != "PROPOSAL":
            raise StrategyLabError("NO_TRADE cannot register a holdout")
        policy = self._evaluation_policy(proposal, spec, start, end, commission_rate, slippage_bps)
        with self.store.db.transaction():
            prior = self.store.strategy_lab.holdout(proposal_id)
            if prior is not None:
                if {k: v for k, v in prior.items() if k != "registered_at"} != policy:
                    raise StrategyLabError("holdout is immutable; its window and policy cannot be changed")
                return prior
            if start <= now:
                raise StrategyLabError("holdout must be registered before its data window begins")
            instrument = self.instruments[proposal["instrument_id"]]
            market = self.assessment.markets.get(instrument.market)
            if instrument.market != spec.market or not instrument.tradable \
                    or spec.parameters.allow_short and instrument.shortable is not True \
                    or instrument.market not in self.sessions:
                raise StrategyLabError("holdout instrument or session eligibility is unavailable")
            local_now = now.astimezone(ZoneInfo(instrument.timezone)).date()
            local_start = start.astimezone(ZoneInfo(instrument.timezone)).date()
            local_end = end.astimezone(ZoneInfo(instrument.timezone)).date()
            days = pd.date_range(local_now, local_start, freq="D")
            if any(not market.is_covered(day.date())
                   for day in pd.date_range(local_now, local_end, freq="D")) \
                    or not any(market.calendar.is_trading_day(day.date())
                               for day in days if local_now < day.date() < local_start):
                raise StrategyLabError("holdout requires a covered full-session embargo after registration")
            zone = ZoneInfo(instrument.timezone)
            if not market.calendar.is_trading_day(local_start) \
                    or not market.calendar.is_trading_day(local_end) \
                    or start != datetime.combine(local_start, market.calendar.open_time, zone) \
                    or end != datetime.combine(local_end, market.calendar.close_time_on(local_end), zone):
                raise StrategyLabError("holdout must begin at session open and end at exchange close")
            if any(datetime.fromisoformat(e["end"]) >= start
                   for e in self.store.strategy_lab.evaluations(proposal_id)):
                raise StrategyLabError("holdout overlaps previously evaluated data")
            self.store.strategy_lab.save_holdout(policy, now)
        result = self.store.strategy_lab.holdout(proposal_id)
        assert result is not None
        return result

    def evaluate(
        self, proposal_id: str, *, start: datetime, end: datetime,
        commission_rate: float = .001, slippage_bps: float = 5,
        evaluation_kind: str = "HISTORICAL",
    ) -> dict[str, Any]:
        now = self.clock()
        ts(start), ts(end), ts(now)
        if start >= end or end > now or end - start > timedelta(days=180):
            raise StrategyLabError("evaluation must use past data over at most 180 days")
        self._validate_costs(commission_rate, slippage_bps)
        if evaluation_kind not in {"HISTORICAL", "POST_PROPOSAL_HOLDOUT"}:
            raise StrategyLabError("unsupported evaluation kind")
        proposal = self.store.strategy_lab.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        spec = StrategyProposalSchema.model_validate(proposal["specification"])
        if spec.outcome != "PROPOSAL":
            raise StrategyLabError("NO_TRADE hypotheses cannot be evaluated for execution")
        instrument = self.instruments[proposal["instrument_id"]]
        if instrument.market != spec.market or not instrument.tradable:
            raise StrategyLabError("instrument market or trading eligibility changed")
        if spec.parameters.allow_short and instrument.shortable is not True:
            raise StrategyLabError("short-selling eligibility is unavailable")
        session = self.sessions.get(instrument.market)
        if session is None:
            raise StrategyLabError("verified session configuration is unavailable")
        market = self.assessment.markets.get(instrument.market)
        holdout = self.store.strategy_lab.holdout(proposal_id)
        policy = self._evaluation_policy(proposal, spec, start, end, commission_rate, slippage_bps)
        if evaluation_kind == "POST_PROPOSAL_HOLDOUT":
            if holdout is None:
                raise StrategyLabError("post-proposal evaluation requires a preregistered future holdout")
            if {k: v for k, v in holdout.items() if k != "registered_at"} != policy:
                raise StrategyLabError("holdout window or frozen evaluation policy changed")
            previous = [e for e in self.store.strategy_lab.evaluations(proposal_id)
                        if e.get("evaluation_kind") == "POST_PROPOSAL_HOLDOUT"]
            if previous:
                return previous[0]
        elif holdout is not None and end >= datetime.fromisoformat(holdout["start"]):
            raise StrategyLabError("historical exploration cannot consume reserved holdout data")
        execution = ExecutionModel(
            commission_rate=commission_rate, slippage_bps=slippage_bps,
            spread_bps=2, participation_rate=.01)
        bars = self.market_data.get_ohlcv(
            instrument, spec.timeframe, start, end)
        quality = validate_bars(
            bars, spec.timeframe, timezone=instrument.timezone,
            requested_start=start, requested_end=end)
        if not quality.ok:
            raise StrategyLabError("historical data validation failed: " + ", ".join(quality.issues))
        bars = completed_intraday_bars(bars, spec.timeframe, end)
        if bars.empty or bars.index.min().to_pydatetime() < start \
                or bars.index.max().to_pydatetime() > end:
            raise StrategyLabError("historical data is empty or outside the requested range")
        dates = session_dates({instrument.instrument_id: bars},
                              {instrument.instrument_id: instrument})
        if any(not market.is_covered(day) for day in dates):
            raise StrategyLabError("exchange calendar coverage is unavailable")
        if evaluation_kind == "POST_PROPOSAL_HOLDOUT":
            zone = ZoneInfo(instrument.timezone)
            expected_dates = {day.date() for day in pd.date_range(
                start.astimezone(zone).date(), end.astimezone(zone).date(), freq="D")
                if market.calendar.is_trading_day(day.date())}
            if set(dates) != expected_dates:
                raise StrategyLabError("holdout is missing complete trading sessions")
            for day in dates:
                opened = datetime.combine(day, market.calendar.open_time, zone)
                close = datetime.combine(day, market.calendar.close_time_on(day), zone)
                expected = pd.date_range(opened, close, freq=interval_delta(spec.timeframe),
                                         inclusive="left")
                expected = expected[[market.calendar.is_open(t) for t in expected]]
                if not expected.isin(bars.index).all():
                    raise StrategyLabError(f"holdout session has missing bars: {day}")
        strategy = self._strategy(spec)
        step = interval_delta(spec.timeframe)

        def signal_fn(iid: str, history: pd.DataFrame) -> BacktestSignal | None:
            if iid != instrument.instrument_id:
                raise StrategyLabError("historical signal instrument mismatch")
            at = history.index[-1].to_pydatetime() + step
            signal = strategy.evaluate(instrument, history, session, as_of=at)
            if signal.intent not in {TradeIntent.BUY, TradeIntent.SHORT_SELL}:
                return None
            if signal.stop_loss is None:
                raise StrategyLabError("deterministic entry lacks a stop")
            return BacktestSignal(
                OrderSide.BUY if signal.intent is TradeIntent.BUY else OrderSide.SELL,
                signal.stop_loss, signal.take_profit, "; ".join(signal.reasons))

        engine = BacktestEngine(BacktestConfig(
            starting_cash=100_000, sizing=self.sizing, base_currency=instrument.currency,
            execution=execution, allow_short=spec.parameters.allow_short,
            session_flatten_before_close=timedelta(minutes=self.flatten_minutes),
            bar_interval=step))
        inputs = {instrument.instrument_id: instrument}
        data = {instrument.instrument_id: bars}
        windows = make_windows(dates, train=20, validate=10, test=10, step=10, embargo=1) \
            if evaluation_kind == "HISTORICAL" else []
        folds = []
        # Freeze the proposed parameters before viewing held-out results. Never select on TEST.
        for window in windows:
            segments = {}
            for name in ("train", "validate", "test"):
                lo, hi = getattr(window, name)
                result = engine.run(inputs, data, signal_fn,
                                    calendars={instrument.instrument_id: market.calendar},
                                    start=lo, end=hi)
                segments[name] = {"metrics": result.metrics, "skipped": result.skipped}
            folds.append({"window": window, **segments})
        oos_count = sum(f["test"]["metrics"]["trades"] for f in folds)
        holdout_result = None
        if evaluation_kind == "POST_PROPOSAL_HOLDOUT":
            result = engine.run(inputs, data, signal_fn,
                                calendars={instrument.instrument_id: market.calendar})
            holdout_result = {
                "metrics": result.metrics, "skipped": result.skipped,
                "trades": result.trades, "session_count": len(dates),
            }
            oos_count = len(result.trades)
            status = "HOLDOUT_REVIEW_REQUIRED" if len(dates) >= 20 and oos_count >= 30 \
                else "INSUFFICIENT_EVIDENCE"
        else:
            status = "EXPLORATORY_NOT_ELIGIBLE" if folds and oos_count >= 30 \
                else "INSUFFICIENT_EVIDENCE"
        bars_hash = hashlib.sha256(bars.to_csv().encode()).hexdigest()
        payload = {
            "proposal_id": proposal_id, "strategy_version_id": proposal["strategy_version_id"],
            "status": status, "executable": False, "bars_hash": bars_hash,
            "start": ts(start), "end": ts(end), "provider": self.market_data.name,
            "costs": {"commission_rate": commission_rate, "slippage_bps": slippage_bps,
                      "spread_bps": 2, "participation_rate": .01, "latency_bars": 1},
            "sizing": self.sizing, "folds": folds, "held_out_trade_count": oos_count,
            "evaluation_kind": evaluation_kind, "evaluation_policy": policy,
            "registered_at": holdout["registered_at"] if evaluation_kind == "POST_PROPOSAL_HOLDOUT"
                and holdout is not None else None,
            "holdout_result": holdout_result,
            "session_flatten": {
                "minutes_before_exchange_close": self.flatten_minutes,
                "price": "SCHEDULED_BOUNDARY_BAR_OPEN", "early_closes": True,
                "bar_alignment": "AT_OR_BEFORE_CUTOFF",
            },
            "baseline": "NO_TRADE: zero trades and zero net profit",
            "limitations": [
                "Historical folds do not prove post-proposal out-of-sample performance.",
                "Historical folds require 42 covered sessions; preregistered holdouts require "
                "20 full sessions and 30 trades for review, not automatic approval.",
                "No activation policy; proposals cannot enter the order pipeline.",
                "Single-instrument local-currency evaluation; no historical FX conversion.",
                "Session flatten uses bar-open simulation, not an intrabar live execution guarantee.",
                "A preregistered holdout is frozen once; multi-proposal selection bias still needs review.",
            ],
        }
        identity = hashlib.sha256(to_json(payload).encode()).hexdigest()
        payload["evaluation_id"] = identity
        self.store.strategy_lab.save_evaluation(payload, now)
        return json.loads(to_json(payload))
