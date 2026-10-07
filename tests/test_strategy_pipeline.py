from datetime import time, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

import pandas as pd

from stockmarket.core import (
    AggregatedAction,
    MarketSession,
    OrbVwapStrategy,
    PipelineStatus,
    ResearchEvidence,
    StrategyResearchPipeline,
)
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.data import (
    DataPolicy,
    MockProvider,
    ResilientProvider,
)
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, RiskDecisionStatus
from stockmarket.core.resilience import RetryPolicy
from stockmarket.core.executors import TradingMode
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.risk import RiskEngine, RiskLimits
from stockmarket.core.trading_service import TradingService


ZONE = "America/New_York"
LOCAL_TIMES = pd.date_range("2026-10-05 09:30", periods=25, freq="5min", tz=ZONE)
AS_OF = LOCAL_TIMES[-1].to_pydatetime() + timedelta(minutes=5)


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def make_session():
    return MarketSession(
        timezone=ZONE,
        market_open=time(9, 30),
        opening_range_end=time(9, 45),
        entry_cutoff=time(15, 0),
        square_off=time(15, 55),
        market_close=time(16, 0),
        late_entry_start=time(12, 0),
    )


def make_bars():
    closes = [100.0 + index * 0.1 for index in range(len(LOCAL_TIMES))]
    closes[-1] += 0.5
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 0.05 for close in closes],
            "low": [close - 0.05 for close in closes],
            "close": closes,
            "volume": [100.0] * (len(closes) - 1) + [500.0],
        },
        index=LOCAL_TIMES,
    )


def selection_response(strategy="orb_vwap"):
    return json.dumps({
        "summary": "The current price action supports evaluating the listed candidate.",
        "ranked_strategies": [{
            "strategy": strategy,
            "confidence": 0.8,
            "rationale": "Recent price and volume context fit the candidate.",
        }],
        "risks": ["Intraday conditions can change."],
        "data_gaps": [],
    })


class CountingMockProvider(MockProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bar_calls = 0

    def get_ohlcv(self, instrument, interval, start, end):
        self.bar_calls += 1
        return super().get_ohlcv(instrument, interval, start, end)


class StaticAIProvider:
    name = "static-test"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self.response


class CapturingOrderManager:
    def __init__(self):
        self.requests = []
        self.decisions = []

    def orders(self):
        return []

    def submit(self, request, decision):
        self.requests.append(request)
        self.decisions.append(decision)
        return SimpleNamespace(
            order=SimpleNamespace(
                is_terminal=True,
                broker_order_id=None,
                client_order_id=request.client_order_id,
            ),
            duplicate=False,
        )


class StrategyResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()
        self.raw_provider = CountingMockProvider(
            {self.instrument.instrument_id: self.instrument},
            {self.instrument.instrument_id: make_bars()},
            clock=lambda: AS_OF.astimezone(timezone.utc),
        )
        self.provider = ResilientProvider(
            self.raw_provider,
            policy=DataPolicy(retry=RetryPolicy(max_attempts=1)),
            clock=lambda: AS_OF.astimezone(timezone.utc),
            sleep=lambda _: None,
        )
        self.addCleanup(self.provider.close)

    def pipeline(self, response=None, *, trading_service=None):
        ai_provider = StaticAIProvider(response or selection_response())
        analyst = AIAnalyst(
            ai_provider,
            clock=lambda: AS_OF.astimezone(timezone.utc),
        )
        pipeline = StrategyResearchPipeline(
            self.provider,
            analyst,
            {"orb_vwap": OrbVwapStrategy()},
            trading_service=trading_service,
        )
        return pipeline, ai_provider

    def evidence(self, component, score=0.8, **overrides):
        values = {
            "instrument_id": self.instrument.instrument_id,
            "component": component,
            "score": score,
            "observed_at": AS_OF - timedelta(minutes=1),
            "source": "test-research",
        }
        values.update(overrides)
        return ResearchEvidence(**values)

    def test_runs_provider_regime_ai_selection_strategy_and_aggregation(self):
        pipeline, ai_provider = self.pipeline()

        result = pipeline.run(self.instrument, make_session(), as_of=AS_OF)

        self.assertIs(result.status, PipelineStatus.COMPLETE)
        self.assertEqual(self.raw_provider.bar_calls, 1)
        self.assertEqual(ai_provider.calls, 1)
        self.assertEqual(result.regime.label.value, "TRENDING_UP")
        self.assertEqual(result.selection.ranked_strategies[0].strategy, "orb_vwap")
        self.assertEqual(result.strategy_signal.side.value, "BUY")
        self.assertIs(result.decision.action, AggregatedAction.SKIP)
        self.assertIn("INSUFFICIENT_COMPONENTS", result.decision.reason_codes)

    def test_data_quality_failure_stops_before_ai_or_strategy(self):
        self.raw_provider.set_bars(
            self.instrument.instrument_id, make_bars().iloc[::-1])
        pipeline, ai_provider = self.pipeline()

        result = pipeline.run(self.instrument, make_session(), as_of=AS_OF)

        self.assertIs(result.status, PipelineStatus.DATA_UNAVAILABLE)
        self.assertIn("UNSORTED_TIMESTAMPS", result.reason)
        self.assertEqual(ai_provider.calls, 0)
        self.assertIsNone(result.strategy_signal)
        self.assertIsNone(result.decision)

    def test_insufficient_regime_history_stops_before_ai(self):
        self.raw_provider.set_bars(
            self.instrument.instrument_id, make_bars().tail(5))
        pipeline, ai_provider = self.pipeline()

        result = pipeline.run(self.instrument, make_session(), as_of=AS_OF)

        self.assertIs(result.status, PipelineStatus.REGIME_UNAVAILABLE)
        self.assertIn("need 21 bars", result.reason)
        self.assertEqual(ai_provider.calls, 0)

    def test_ai_failure_or_invented_candidate_produces_no_signal(self):
        pipeline, ai_provider = self.pipeline(selection_response("invented"))

        result = pipeline.run(self.instrument, make_session(), as_of=AS_OF)

        self.assertIs(result.status, PipelineStatus.AI_UNAVAILABLE)
        self.assertIn("unknown strategy candidate", result.reason)
        self.assertEqual(ai_provider.calls, 1)
        self.assertIsNone(result.strategy_signal)
        self.assertIsNone(result.decision)

    def test_registry_must_match_deterministic_strategy_names(self):
        with self.assertRaisesRegex(ValueError, "must match strategy.name"):
            StrategyResearchPipeline(
                self.provider,
                AIAnalyst(StaticAIProvider(selection_response())),
                {"other_name": OrbVwapStrategy()},
            )

    def test_timezone_mismatch_is_rejected_before_provider_access(self):
        pipeline, ai_provider = self.pipeline()
        bad_session = MarketSession(
            timezone="UTC",
            market_open=time(9, 30),
            opening_range_end=time(9, 45),
            entry_cutoff=time(15, 0),
            square_off=time(15, 55),
            market_close=time(16, 0),
            late_entry_start=time(12, 0),
        )

        with self.assertRaisesRegex(ValueError, "timezone must match"):
            pipeline.run(self.instrument, bad_session, as_of=AS_OF)

        self.assertEqual(self.raw_provider.bar_calls, 0)
        self.assertEqual(ai_provider.calls, 0)

    def test_fresh_research_evidence_can_complete_aggregation(self):
        pipeline, _ = self.pipeline()

        result = pipeline.run(
            self.instrument,
            make_session(),
            as_of=AS_OF,
            research_evidence=(
                self.evidence("volume", 0.9),
                self.evidence("momentum", 0.8),
            ),
        )

        self.assertIs(result.status, PipelineStatus.COMPLETE)
        self.assertIs(result.decision.action, AggregatedAction.BUY)
        self.assertEqual(result.decision.explanation["inputs"]["volume"], 0.9)
        self.assertEqual(result.decision.explanation["inputs"]["momentum"], 0.8)

    def test_mismatched_stale_future_and_duplicate_research_fail_closed(self):
        pipeline, ai_provider = self.pipeline()
        scenarios = (
            ((self.evidence("volume", instrument_id="other"),), "INSTRUMENT_MISMATCH"),
            ((self.evidence("volume", observed_at=AS_OF - timedelta(minutes=6)),), "STALE_RESEARCH"),
            ((self.evidence("volume", observed_at=AS_OF + timedelta(seconds=1)),), "RESEARCH_FROM_FUTURE"),
            ((self.evidence("volume"), self.evidence("volume")), "DUPLICATE_RESEARCH_COMPONENT"),
        )

        for evidence, expected_reason in scenarios:
            with self.subTest(expected_reason=expected_reason):
                provider_calls = self.raw_provider.bar_calls
                ai_calls = ai_provider.calls
                result = pipeline.run(
                    self.instrument,
                    make_session(),
                    as_of=AS_OF,
                    research_evidence=evidence,
                )
                self.assertIs(result.status, PipelineStatus.RESEARCH_UNAVAILABLE)
                self.assertIn(expected_reason, result.reason)
                self.assertEqual(self.raw_provider.bar_calls, provider_calls)
                self.assertEqual(ai_provider.calls, ai_calls)

    def test_research_evidence_rejects_invalid_score_component_and_timestamp(self):
        base = {
            "instrument_id": self.instrument.instrument_id,
            "component": "volume",
            "score": 0.5,
            "observed_at": AS_OF,
            "source": "test-research",
        }
        with self.assertRaisesRegex(ValueError, "score must be finite"):
            ResearchEvidence(**{**base, "score": float("nan")})
        with self.assertRaisesRegex(ValueError, "component must be"):
            ResearchEvidence(**{**base, "component": "technical"})
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ResearchEvidence(**{**base, "observed_at": AS_OF.replace(tzinfo=None)})

    def make_trading_service(self, order_manager, quote_price, mode=TradingMode.PAPER):
        risk_engine = RiskEngine(RiskLimits(
            max_position_quantity=100,
            max_order_notional=20_000,
            max_open_positions=3,
            max_trades_per_day=10,
            cash_requirement_rate=1.0,
            market_session=make_session(),
        ))
        portfolio = PortfolioManager("USD", 50_000, timezone=ZONE)
        return TradingService(
            mode=mode,
            risk_engine=risk_engine,
            order_manager=order_manager,
            portfolio=portfolio,
            instruments={self.instrument.instrument_id: self.instrument},
            quotes=lambda _: (quote_price, AS_OF - timedelta(minutes=1)),
            clock=lambda: AS_OF,
        )

    def test_actionable_result_routes_through_paper_service_and_risk_engine(self):
        order_manager = CapturingOrderManager()
        # The quote is a fresh independent input to the RiskEngine, not the AI ranking.
        result_signal_price = float(make_bars()["close"].iloc[-1])
        service = self.make_trading_service(order_manager, result_signal_price)
        pipeline, _ = self.pipeline(trading_service=service)
        result = pipeline.run(
            self.instrument,
            make_session(),
            as_of=AS_OF,
            research_evidence=(
                self.evidence("volume", 0.9),
                self.evidence("momentum", 0.8),
            ),
        )

        submitted = pipeline.submit_decision(result, 1, actor="test")

        self.assertEqual(len(order_manager.requests), 1)
        self.assertIs(order_manager.decisions[0].status, RiskDecisionStatus.APPROVED)
        self.assertEqual(order_manager.requests[0].signal_id, result.strategy_signal.signal_id)
        self.assertEqual(submitted.risk.status, RiskDecisionStatus.APPROVED)

    def test_risk_rejection_is_preserved_and_skip_is_never_submitted(self):
        order_manager = CapturingOrderManager()
        result_signal_price = float(make_bars()["close"].iloc[-1])
        service = self.make_trading_service(order_manager, result_signal_price)
        pipeline, _ = self.pipeline(trading_service=service)
        actionable = pipeline.run(
            self.instrument,
            make_session(),
            as_of=AS_OF,
            research_evidence=(
                self.evidence("volume", 0.9),
                self.evidence("momentum", 0.8),
            ),
        )
        rejected = pipeline.submit_decision(actionable, 101)
        self.assertIs(rejected.risk.status, RiskDecisionStatus.REJECTED)
        self.assertIn("MAX_POSITION_QUANTITY_EXCEEDED", rejected.risk.reason)

        no_evidence, _ = self.pipeline(trading_service=service)
        skipped = no_evidence.run(self.instrument, make_session(), as_of=AS_OF)
        before = len(order_manager.requests)
        with self.assertRaisesRegex(ValueError, "only BUY or SELL"):
            no_evidence.submit_decision(skipped, 1)
        self.assertEqual(len(order_manager.requests), before)

    def test_pipeline_submission_refuses_live_mode(self):
        order_manager = CapturingOrderManager()
        quote_price = float(make_bars()["close"].iloc[-1])
        service = self.make_trading_service(
            order_manager, quote_price, mode=TradingMode.LIVE)
        pipeline, _ = self.pipeline(trading_service=service)
        result = pipeline.run(
            self.instrument,
            make_session(),
            as_of=AS_OF,
            research_evidence=(
                self.evidence("volume", 0.9),
                self.evidence("momentum", 0.8),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "PAPER mode"):
            pipeline.submit_decision(result, 1)
        self.assertEqual(order_manager.requests, [])


if __name__ == "__main__":
    unittest.main()
