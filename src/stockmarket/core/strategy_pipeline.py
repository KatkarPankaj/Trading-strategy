"""Fail-closed composition of market data, research, strategy and aggregation stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Sequence

import pandas as pd

from .aggregation import (
    AggregatedAction,
    AggregatedDecision,
    SignalAggregator,
    SignalInputs,
)
from .ai.analyst import AIAnalyst, AIResult, StrategySelection
from .data.provider import DataProviderError, interval_delta
from .data.resilient import ResilientProvider
from .executors import TradingMode
from .market_session import MarketSession
from .models import Instrument, Signal, SignalSide
from .regime import (
    MarketRegimeEvaluator,
    RegimeAssessment,
    RegimeConfig,
    RegimeUnavailable,
    regime_score,
)
from .research import ResearchEvidence
from .strategies.base import Strategy
from .trading_service import TicketResult, TradingService


class PipelineStatus(str, Enum):
    COMPLETE = "COMPLETE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    REGIME_UNAVAILABLE = "REGIME_UNAVAILABLE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    RESEARCH_UNAVAILABLE = "RESEARCH_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class StrategyPipelineConfig:
    interval: str = "5m"
    lookback: timedelta = timedelta(hours=4)
    max_research_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        interval_delta(self.interval)
        if not isinstance(self.lookback, timedelta) or self.lookback <= timedelta(0):
            raise ValueError("lookback must be a positive timedelta")
        if not isinstance(self.max_research_age, timedelta) \
                or self.max_research_age <= timedelta(0):
            raise ValueError("max_research_age must be a positive timedelta")


@dataclass(frozen=True, slots=True)
class StrategyPipelineResult:
    status: PipelineStatus
    reason: str | None
    bars: pd.DataFrame | None = None
    regime: RegimeAssessment | None = None
    ai_result: AIResult | None = None
    selection: StrategySelection | None = None
    strategy_signal: Signal | None = None
    decision: AggregatedDecision | None = None


class StrategyResearchPipeline:
    """Run research-selected deterministic strategies without creating orders.

    The AI stage is required for strategy selection. Its confidence is advisory only;
    selected strategy signals remain the only source of technical direction.
    """

    def __init__(
        self,
        provider: ResilientProvider,
        analyst: AIAnalyst,
        strategies: Mapping[str, Strategy],
        *,
        config: StrategyPipelineConfig | None = None,
        regime_evaluator: MarketRegimeEvaluator | None = None,
        aggregator: SignalAggregator | None = None,
        trading_service: TradingService | None = None,
    ) -> None:
        self.config = config or StrategyPipelineConfig()
        self.regime_evaluator = regime_evaluator or MarketRegimeEvaluator(
            RegimeConfig(
                interval=self.config.interval,
                max_bar_age=interval_delta(self.config.interval),
            ))
        if self.regime_evaluator.config.interval != self.config.interval:
            raise ValueError("pipeline and regime evaluator intervals must match")
        if not strategies or len(strategies) > 20:
            raise ValueError("strategies must contain between 1 and 20 candidates")
        for name, strategy in strategies.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("strategy registry names must be non-empty strings")
            if not isinstance(strategy, Strategy):
                raise TypeError(f"strategy {name!r} does not implement Strategy")
            if strategy.name != name:
                raise ValueError(
                    f"strategy registry key {name!r} must match strategy.name {strategy.name!r}")
        self.provider = provider
        self.analyst = analyst
        self.strategies = dict(strategies)
        self.aggregator = aggregator or SignalAggregator()
        self.trading_service = trading_service

    def run(
        self,
        instrument: Instrument,
        session: MarketSession,
        *,
        as_of: datetime,
        research_evidence: Sequence[ResearchEvidence] = (),
    ) -> StrategyPipelineResult:
        """Run one data snapshot through research and deterministic aggregation."""
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        if session.timezone != instrument.timezone:
            raise ValueError("session timezone must match instrument timezone")
        evidence, evidence_error = self._validate_research(
            research_evidence, instrument, as_of)
        if evidence_error is not None:
            return StrategyPipelineResult(
                PipelineStatus.RESEARCH_UNAVAILABLE, evidence_error)

        try:
            bars = self.provider.get_ohlcv(
                instrument,
                self.config.interval,
                as_of - self.config.lookback,
                as_of,
            )
        except DataProviderError as exc:
            detail = ",".join(exc.issues) if hasattr(exc, "issues") else type(exc).__name__
            return StrategyPipelineResult(
                PipelineStatus.DATA_UNAVAILABLE, f"MARKET_DATA_UNAVAILABLE:{detail}")

        try:
            regime = self.regime_evaluator.evaluate(
                instrument, bars, as_of=as_of)
        except RegimeUnavailable as exc:
            return StrategyPipelineResult(
                PipelineStatus.REGIME_UNAVAILABLE,
                f"REGIME_UNAVAILABLE:{exc}",
                bars=bars,
            )
        except DataProviderError as exc:
            detail = ",".join(exc.issues) if hasattr(exc, "issues") else type(exc).__name__
            return StrategyPipelineResult(
                PipelineStatus.DATA_UNAVAILABLE,
                f"MARKET_DATA_INVALID:{detail}",
                bars=bars,
            )

        research_context = {
            "regime": {
                "label": regime.label.value,
                "directional_score": regime.directional_score,
                "volatility": regime.volatility,
                "interval": regime.interval,
                "lookback_bars": regime.lookback_bars,
            },
            "latest_bars": [
                {
                    "timestamp": timestamp.isoformat(),
                    **{key: float(value) for key, value in row.items()},
                }
                for timestamp, row in bars.tail(3).iterrows()
            ],
            "evidence": [
                {
                    "component": item.component,
                    "score": item.score,
                    "observed_at": item.observed_at.isoformat(),
                    "source": item.source,
                }
                for item in evidence
            ],
        }
        ai_result, selection = self.analyst.select_strategies(
            instrument_id=instrument.instrument_id,
            as_of=as_of,
            available_strategies=tuple(self.strategies),
            research_context=research_context,
        )
        if not ai_result.ok or selection is None:
            return StrategyPipelineResult(
                PipelineStatus.AI_UNAVAILABLE,
                ai_result.error or "STRATEGY_SELECTION_UNAVAILABLE",
                bars=bars,
                regime=regime,
                ai_result=ai_result,
            )

        selected_name = selection.ranked_strategies[0].strategy
        strategy = self.strategies[selected_name]
        try:
            signal = strategy.evaluate(
                instrument, bars, session, as_of=as_of)
        except DataProviderError as exc:
            detail = ",".join(exc.issues) if hasattr(exc, "issues") else type(exc).__name__
            return StrategyPipelineResult(
                PipelineStatus.DATA_UNAVAILABLE,
                f"STRATEGY_DATA_INVALID:{detail}",
                bars=bars,
                regime=regime,
                ai_result=ai_result,
                selection=selection,
            )

        component_scores = {
            item.component: float(item.score) for item in evidence
        }
        history_evidence = next(
            (item for item in evidence if item.component == "history"), None)
        inputs = SignalInputs(
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            timestamp=signal.timestamp,
            strategy=selected_name,
            **component_scores,
            regime=regime_score(regime, instrument.instrument_id),
            history_trades=history_evidence.history_trades if history_evidence else 0,
            volatility=regime.volatility,
        )
        decision = self.aggregator.aggregate(
            inputs, as_of=as_of, strategy_signal=signal)
        return StrategyPipelineResult(
            PipelineStatus.COMPLETE,
            None,
            bars=bars,
            regime=regime,
            ai_result=ai_result,
            selection=selection,
            strategy_signal=signal,
            decision=decision,
        )

    def submit_decision(
        self,
        result: StrategyPipelineResult,
        quantity: int,
        *,
        actor: str = "system",
    ) -> TicketResult:
        """Explicitly submit an actionable research result through PAPER TradingService."""
        if self.trading_service is None:
            raise RuntimeError("TradingService is not configured for this pipeline")
        if self.trading_service.mode is not TradingMode.PAPER:
            raise RuntimeError("strategy pipeline submissions are restricted to PAPER mode")
        if not isinstance(result, StrategyPipelineResult) \
                or result.status is not PipelineStatus.COMPLETE \
                or result.decision is None or result.strategy_signal is None:
            raise ValueError("a complete pipeline result with a deterministic signal is required")
        expected_side = {
            AggregatedAction.BUY: SignalSide.BUY,
            AggregatedAction.SELL: SignalSide.SELL,
        }.get(result.decision.action)
        if expected_side is None:
            raise ValueError("only BUY or SELL aggregated decisions can be submitted")
        if result.strategy_signal.side is not expected_side:
            raise ValueError("aggregated action must match the deterministic strategy signal")
        return self.trading_service.submit_signal(
            result.strategy_signal, quantity, actor=actor)

    def _validate_research(
        self,
        research_evidence: Sequence[ResearchEvidence],
        instrument: Instrument,
        as_of: datetime,
    ) -> tuple[tuple[ResearchEvidence, ...], str | None]:
        if not isinstance(research_evidence, Sequence) \
                or isinstance(research_evidence, (str, bytes)):
            raise TypeError("research_evidence must be a sequence of ResearchEvidence")
        items = tuple(research_evidence)
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, ResearchEvidence):
                raise TypeError("research_evidence entries must be ResearchEvidence")
            if item.instrument_id != instrument.instrument_id:
                return (), "RESEARCH_INSTRUMENT_MISMATCH"
            if item.component in seen:
                return (), f"DUPLICATE_RESEARCH_COMPONENT:{item.component}"
            seen.add(item.component)
            age = as_of - item.observed_at
            if age < timedelta(0):
                return (), f"RESEARCH_FROM_FUTURE:{item.component}"
            if age > self.config.max_research_age:
                return (), f"STALE_RESEARCH:{item.component}"
        return items, None
