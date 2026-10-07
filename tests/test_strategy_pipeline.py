from datetime import time, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

import pandas as pd

from stockmarket.core import (
    AggregatedAction,
    BrokerConstraints,
    MarketSession,
    OrbVwapStrategy,
    PipelineStatus,
    PortfolioRiskLimits,
    ResearchEvidence,
    SizingLimits,
    StrategyResearchPipeline,
)
from stockmarket.core.market_intelligence import MarketIntelligenceOrchestrator
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.audit_trail import reconstruct
from stockmarket.core.recovery import RecoveryManager, rebuild_portfolio
from stockmarket.core.data import (
    DataPolicy,
    MockProvider,
    ResilientProvider,
)
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, OrderSide, RiskDecisionStatus
from stockmarket.core.resilience import RetryPolicy
from stockmarket.core.brokers import PaperBroker
from stockmarket.core.executors import TradingMode
from stockmarket.core.order_management import OrderManager
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.risk import RiskEngine, RiskLimits
from stockmarket.core.risk_portfolio import CONTROLS
from stockmarket.core.research import NewsEvidenceProducer
from stockmarket.core.research import (
    FundamentalEvidenceProducer,
    ResearchEvidenceCollection,
    ResearchObservation,
    SectorEvidenceProducer,
)
from stockmarket.core.trading_gate import TradingGate
from stockmarket.core.trading_service import TradingService
from stockmarket.news import (
    MarketImpact,
    NewsEvent,
    NewsEventType,
    NewsSentiment,
)


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
        if '"directional_score"' in system:
            return json.dumps({
                "summary": "The cited observation is supportive.",
                "directional_score": 0.8,
                "confidence": 0.75,
                "risks": [],
                "data_gaps": [],
            })
        return self.response


class StaticResearchProvider:
    name = "research-test"

    def __init__(self, component):
        self.component = component

    def get_observation(self, instrument, *, component, as_of, max_age):
        return ResearchObservation(
            instrument_id=instrument.instrument_id,
            market=instrument.market,
            component=self.component,
            subject="Technology" if component == "sector" else "FY2026 results",
            content="Validated research facts from the configured test source.",
            observed_at=as_of - timedelta(minutes=1),
            source="research-test",
        )


class StaticNewsProvider:
    def __init__(self, events):
        self.events = events

    def get_news(self, query):
        return self.events


class CapturingOrderManager:
    def __init__(self):
        self.requests = []
        self.decisions = []
        self.active_orders = []

    def orders(self):
        return []

    def open_orders(self):
        return self.active_orders

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


class StaticNewsEvidenceProducer:
    def collect(self, instrument, *, as_of):
        return ResearchEvidenceCollection(
            evidence=(ResearchEvidence(
                instrument_id=instrument.instrument_id,
                component="news",
                score=0.8,
                observed_at=as_of - timedelta(minutes=1),
                source="test-news",
            ),),
            event_count=1,
            analyzed_count=1,
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

    def pipeline(self, response=None, *, trading_service=None,
                 research_evidence_producers=()):
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
            research_evidence_producers=research_evidence_producers,
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

    def test_market_intelligence_ranks_deterministically_and_keeps_submission_context(self):
        second_instrument = default_markets().get("US").instrument(
            "MSFT", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
        raw_provider = CountingMockProvider(
            {
                self.instrument.instrument_id: self.instrument,
                second_instrument.instrument_id: second_instrument,
            },
            {
                self.instrument.instrument_id: make_bars(),
                second_instrument.instrument_id: make_bars(),
            },
            clock=lambda: AS_OF.astimezone(timezone.utc),
        )
        provider = ResilientProvider(
            raw_provider,
            policy=DataPolicy(retry=RetryPolicy(max_attempts=1)),
            clock=lambda: AS_OF.astimezone(timezone.utc),
            sleep=lambda _: None,
        )
        self.addCleanup(provider.close)
        pipeline = StrategyResearchPipeline(
            provider,
            AIAnalyst(
                StaticAIProvider(selection_response()),
                clock=lambda: AS_OF.astimezone(timezone.utc),
            ),
            {"orb_vwap": OrbVwapStrategy()},
            news_evidence_producer=StaticNewsEvidenceProducer(),
        )
        orchestrator = MarketIntelligenceOrchestrator(
            pipeline,
            clock=lambda: AS_OF + timedelta(seconds=1),
        )

        result = orchestrator.run(
            ((second_instrument, make_session()), (self.instrument, make_session())),
            as_of=AS_OF,
        )

        self.assertEqual(
            [item.instrument_id for item in result.proposals],
            sorted((self.instrument.instrument_id, second_instrument.instrument_id)),
        )
        self.assertEqual(
            [item.proposal.proposal_id for item in result.submission_contexts],
            [item.proposal_id for item in result.proposals],
        )
        self.assertTrue(all(
            item.risk_status == "NOT_EVALUATED" for item in result.proposals))

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

    def test_news_producer_contributes_to_pipeline_aggregation(self):
        event = NewsEvent(
            timestamp=AS_OF - timedelta(minutes=30),
            source="exchange",
            headline="Company announces a major contract",
            event_type=NewsEventType.CONTRACT,
            sentiment=NewsSentiment.UNKNOWN,
            sentiment_confidence=0.0,
            market_impact=MarketImpact.HIGH,
            relevance=0.9,
            symbol=self.instrument.symbol,
            affected_market=self.instrument.market,
        )
        news_analyst = AIAnalyst(
            StaticAIProvider(json.dumps({
                "summary": "A positive contract announcement.",
                "event_type": "contract",
                "sentiment": "positive",
                "sentiment_confidence": 0.9,
                "market_impact": "high",
                "relevance": 0.8,
                "key_points": ["Contract announced"],
                "risks": [],
            })),
            clock=lambda: AS_OF.astimezone(timezone.utc),
        )
        producer = NewsEvidenceProducer(
            StaticNewsProvider([event]),
            news_analyst,
            max_age=timedelta(hours=1),
        )
        pipeline, _ = self.pipeline()
        pipeline.news_evidence_producer = producer

        result = pipeline.run(self.instrument, make_session(), as_of=AS_OF)

        self.assertIs(result.status, PipelineStatus.COMPLETE)
        self.assertAlmostEqual(result.decision.explanation["inputs"]["news"], 0.72)
        provenance = result.decision.explanation["inputs"]["research_provenance"]["news"]
        self.assertEqual(provenance["observed_at"], event.timestamp.isoformat())
        self.assertTrue(provenance["source"].startswith("ai_news:"))
        self.assertIs(result.decision.action, AggregatedAction.BUY)

    def test_sector_and_fundamental_producers_contribute_to_selection_and_aggregation(self):
        ai_provider = StaticAIProvider(selection_response())
        analyst = AIAnalyst(
            ai_provider,
            clock=lambda: AS_OF.astimezone(timezone.utc),
        )
        sector = SectorEvidenceProducer(
            StaticResearchProvider("sector"),
            analyst,
            max_age=timedelta(days=1),
        )
        fundamental = FundamentalEvidenceProducer(
            StaticResearchProvider("fundamental"),
            analyst,
            max_age=timedelta(days=90),
        )
        pipeline = StrategyResearchPipeline(
            self.provider,
            analyst,
            {"orb_vwap": OrbVwapStrategy()},
            research_evidence_producers=(sector, fundamental),
        )

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
        self.assertEqual(
            {item.component for item in result.research_evidence},
            {"volume", "momentum", "sector", "fundamental"},
        )
        self.assertEqual(ai_provider.calls, 3)
        self.assertIs(result.decision.action, AggregatedAction.BUY)
        self.assertEqual(result.research_warnings, ())

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

    def make_trading_service(
        self, order_manager, quote_price, mode=TradingMode.PAPER,
        portfolio_limits=None,
    ):
        risk_engine = RiskEngine(RiskLimits(
            max_position_quantity=100,
            max_order_notional=20_000,
            max_open_positions=3,
            max_trades_per_day=10,
            cash_requirement_rate=1.0,
            market_session=make_session(),
        ), portfolio_limits)
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

    def make_real_paper_service(self, starting_cash, store=None):
        portfolio = PortfolioManager("USD", starting_cash, timezone=ZONE)
        if store is not None:
            portfolio.on_fill = store.fills.save
        broker = PaperBroker(
            portfolio,
            {self.instrument.instrument_id: self.instrument},
            clock=lambda: AS_OF,
            market_status_fn=lambda _: True,
        )
        broker.connect()
        broker.update_price(
            self.instrument.instrument_id,
            float(make_bars()["close"].iloc[-1]),
            AS_OF - timedelta(minutes=1),
        )
        risk_engine = RiskEngine(RiskLimits(
            max_position_quantity=100,
            max_order_notional=20_000,
            max_open_positions=3,
            max_trades_per_day=10,
            cash_requirement_rate=1.0,
            market_session=make_session(),
        ))
        order_manager = OrderManager(broker, clock=lambda: AS_OF)
        service = TradingService(
            mode=TradingMode.PAPER,
            risk_engine=risk_engine,
            order_manager=order_manager,
            portfolio=portfolio,
            instruments={self.instrument.instrument_id: self.instrument},
            quotes=lambda iid: broker.last_quote(iid),
            store=store,
            clock=lambda: AS_OF,
            sizing_limits=SizingLimits(
                risk_per_trade_pct=0.01,
                max_order_notional=20_000,
                max_position_notional_pct=0.2,
                max_total_notional_pct=1.0,
                max_sector_exposure_pct=0.3,
                broker=BrokerConstraints(
                    max_order_quantity=100, max_order_notional=20_000),
            ),
        )
        return service, broker, portfolio, order_manager

    def test_actionable_research_result_cannot_become_an_order(self):
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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(
                result,
                1,
                actor="test",
                client_order_id="proposal-test-idempotency",
                proposal_id="proposal-test",
            )
        self.assertEqual(order_manager.requests, [])

    def test_research_pipeline_always_requires_persisted_risk_proposal(self):
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
        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(actionable, 101)

        no_evidence, _ = self.pipeline(trading_service=service)
        skipped = no_evidence.run(self.instrument, make_session(), as_of=AS_OF)
        before = len(order_manager.requests)
        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            no_evidence.submit_decision(skipped, 1)
        self.assertEqual(len(order_manager.requests), before)

    def test_active_order_reservation_is_included_in_final_portfolio_risk(self):
        order_manager = CapturingOrderManager()
        price = float(make_bars()["close"].iloc[-1])
        portfolio_limits = PortfolioRiskLimits(
            max_risk_per_trade_pct=0.01,
            max_total_notional_pct=0.18,
            disabled={
                name: "not part of this reservation-focused test"
                for name in CONTROLS
                if name not in {
                    "max_risk_per_trade_pct", "max_total_notional_pct"}
            },
        )
        service = self.make_trading_service(
            order_manager, price, portfolio_limits=portfolio_limits)
        order_manager.active_orders.append(SimpleNamespace(
            instrument_id=self.instrument.instrument_id,
            side=OrderSide.BUY,
            remaining_quantity=90,
            quantity=90,
            timestamp=AS_OF - timedelta(seconds=10),
            limit_price=100.0,
            signal_id=None,
            is_terminal=False,
        ))
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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(result, 1)
        self.assertEqual(order_manager.requests, [])

    def test_actionable_pipeline_does_not_reach_real_paper_broker(self):
        service, broker, portfolio, order_manager = self.make_real_paper_service(50_000)
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
        self.assertIs(result.decision.action, AggregatedAction.BUY)

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(result, 1, actor="pipeline-integration-test")
        self.assertEqual(portfolio.fills, ())
        self.assertEqual(portfolio.positions(), {})
        self.assertEqual(broker.open_orders(), ())
        self.assertEqual(order_manager.orders(), ())

    def test_research_pipeline_cannot_use_automatic_order_sizing(self):
        database = SQLiteDatabase()
        migrate(database)
        store = Store(database)
        self.addCleanup(store.db.close)
        store.instruments.save(self.instrument)
        service, _, portfolio, _ = self.make_real_paper_service(50_000, store)
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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(
                result,
                sizing_mode="AUTOMATIC_SIZING",
                actor="automatic-sizing-test",
            )
        self.assertEqual(portfolio.positions(), {})
        self.assertEqual(store.orders.all(), [])

    def test_real_paper_broker_is_not_reached_when_risk_rejects(self):
        service, broker, portfolio, order_manager = self.make_real_paper_service(0)
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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(result, 1)
        self.assertEqual(portfolio.fills, ())
        self.assertEqual(portfolio.positions(), {})
        self.assertEqual(broker.open_orders(), ())
        self.assertEqual(order_manager.open_orders(), ())

    def test_pipeline_result_cannot_persist_or_recover_an_order(self):
        database = SQLiteDatabase()
        migrate(database)
        store = Store(database)
        self.addCleanup(store.db.close)
        store.instruments.save(self.instrument)
        service, broker, portfolio, order_manager = self.make_real_paper_service(
            50_000, store)
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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(result, 1, actor="pipeline-persistence-test")
        self.assertEqual(store.orders.all(), [])
        self.assertEqual(store.fills.all(), [])
        self.assertEqual(store.execution_records.recent(), [])
        self.assertEqual(store.audit.verify_chain(), [])
        self.assertEqual(portfolio.fills, ())
        self.assertEqual(portfolio.positions(), {})
        self.assertEqual(broker.open_orders(), ())
        self.assertEqual(order_manager.open_orders(), ())

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

        with self.assertRaisesRegex(RuntimeError, "persisted approved"):
            pipeline.submit_decision(result, 1)
        self.assertEqual(order_manager.requests, [])


if __name__ == "__main__":
    unittest.main()
