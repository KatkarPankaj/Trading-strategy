from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.core import MarketSession, OrbVwapStrategy
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.data import DataPolicy, MockProvider, ResilientProvider
from stockmarket.core.executors import TradingMode
from stockmarket.core.market_intelligence import (
    MarketIntelligenceResult,
    ProposalSubmissionContext,
    TradeProposal,
)
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, SignalSide
from stockmarket.core.models import OrderSide, OrderStatus, OrderType
from stockmarket.core.order_management import ManagedOrder
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.regime import RegimeAssessment, RegimeLabel
from stockmarket.core.security import Secret
from stockmarket.core.strategy_pipeline import (
    PipelineStatus,
    StrategyPipelineResult,
    StrategyResearchPipeline,
)
from stockmarket.core.ai.analyst import StrategySelection


ZONE = "America/New_York"
TIMES = pd.date_range("2026-10-05 09:30", periods=25, freq="5min", tz=ZONE)
AS_OF = TIMES[-1].to_pydatetime() + timedelta(minutes=5)
TOKEN = "api-research-test-token"


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def make_bars():
    closes = [100.0 + index * 0.1 for index in range(len(TIMES))]
    closes[-1] += 0.5
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 0.05 for close in closes],
            "low": [close - 0.05 for close in closes],
            "close": closes,
            "volume": [100.0] * (len(closes) - 1) + [500.0],
        },
        index=TIMES,
    )


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


class StaticAIProvider:
    name = "api-test-ai"

    def complete(self, system, user):
        if not system or not user:
            raise AssertionError("AI prompts must be non-empty")
        return json.dumps({
            "summary": "The listed strategy merits deterministic evaluation.",
            "ranked_strategies": [{
                "strategy": "orb_vwap",
                "confidence": 0.9,
                "rationale": "The supplied evidence aligns with the known strategy.",
            }],
            "risks": ["Intraday conditions can change."],
            "data_gaps": [],
        })


def make_context(*, configured=True):
    instrument = make_instrument()
    provider = ResilientProvider(
        MockProvider(
            {instrument.instrument_id: instrument},
            {instrument.instrument_id: make_bars()},
            clock=lambda: AS_OF.astimezone(timezone.utc),
        ),
        policy=DataPolicy(),
        clock=lambda: AS_OF.astimezone(timezone.utc),
    )
    pipeline = None
    if configured:
        pipeline = StrategyResearchPipeline(
            provider,
            AIAnalyst(
                StaticAIProvider(),
                clock=lambda: AS_OF.astimezone(timezone.utc),
            ),
            {"orb_vwap": OrbVwapStrategy()},
        )
    db = SQLiteDatabase()
    migrate(db)
    store = Store(db)
    context = ApiContext(
        settings=SimpleNamespace(),
        paper=SimpleNamespace(mode=TradingMode.PAPER),
        live=None,
        store=store,
        health=None,
        api_token=Secret(TOKEN),
        instruments={instrument.instrument_id: instrument},
        research_pipeline=pipeline,
        research_sessions={"US": make_session()},
    )
    return context, provider


class ResearchApiTests(unittest.TestCase):
    def client(self, *, configured=True):
        context, provider = make_context(configured=configured)
        self.addCleanup(provider.close)
        return TestClient(create_app(context))

    def test_research_endpoint_returns_advisory_decision_without_submitting_order(self):
        client = self.client()
        response = client.post(
            "/research",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "instrument_id": "XNAS:AAPL",
                "as_of": AS_OF.isoformat(),
                "evidence": [
                    {
                        "component": "volume",
                        "score": 0.9,
                        "observed_at": (AS_OF - timedelta(minutes=1)).isoformat(),
                        "source": "api-user-research",
                    },
                    {
                        "component": "momentum",
                        "score": 0.8,
                        "observed_at": (AS_OF - timedelta(minutes=1)).isoformat(),
                        "source": "api-user-research",
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["trading_mode"], "PAPER")
        self.assertEqual(payload["status"], "COMPLETE")
        self.assertEqual(payload["decision"]["action"], "BUY")
        self.assertEqual(payload["signal"]["side"], "BUY")
        self.assertEqual(payload["bar_count"], 25)
        self.assertEqual(
            {item["component"] for item in payload["research_evidence"]},
            {"volume", "momentum"},
        )

    def test_research_route_requires_authentication_and_configured_pipeline(self):
        client = self.client(configured=False)
        body = {"instrument_id": "XNAS:AAPL"}

        self.assertEqual(client.post("/research", json=body).status_code, 401)
        response = client.post(
            "/research",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["detail"])

    def test_research_route_rejects_unknown_instrument_and_naive_timestamp(self):
        client = self.client()
        headers = {"Authorization": f"Bearer {TOKEN}"}

        unknown = client.post(
            "/research",
            headers=headers,
            json={"instrument_id": "XNAS:MSFT"},
        )
        self.assertEqual(unknown.status_code, 404)

        naive = client.post(
            "/research",
            headers=headers,
            json={
                "instrument_id": "XNAS:AAPL",
                "as_of": "2026-10-05T11:35:00",
            },
        )
        self.assertEqual(naive.status_code, 422)

    def test_proposal_submission_does_not_accept_ephemeral_research_proposals(self):
        context, provider = make_context()
        self.addCleanup(provider.close)
        self.addCleanup(context.store.db.close)
        context.settings.max_market_data_age_seconds = 300
        as_of = datetime.now(timezone.utc) - timedelta(seconds=2)
        generated_at = as_of + timedelta(seconds=1)
        instrument = next(iter(context.instruments.values()))
        proposal = TradeProposal(
            proposal_id="test-proposal-id",
            rank=1,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            market=instrument.market,
            as_of=as_of,
            generated_at=generated_at,
            side=SignalSide.BUY,
            strategy="orb_vwap",
            signal_id=uuid4(),
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            opportunity_score=75.0,
            aggregate_score=0.5,
            aggregation_explanation='{"action":"BUY"}',
            regime=RegimeAssessment(
                instrument_id=instrument.instrument_id,
                label=RegimeLabel.TRENDING_UP,
                directional_score=0.6,
                volatility=0.01,
                interval="5m",
                lookback_bars=20,
                start_at=as_of - timedelta(minutes=100),
                end_at=as_of,
                assessed_at=as_of,
            ),
            strategy_selection=StrategySelection(
                instrument_id=instrument.instrument_id,
                as_of=as_of,
                generated_at=generated_at,
                provider="test",
                prompt_hash="prompt",
                response_hash="response",
                summary="A test selection.",
                ranked_strategies=(),
                risks=(),
                data_gaps=(),
            ),
            research_evidence=(),
            research_warnings=(),
            explanation=("Deterministic test proposal.",),
        )
        pipeline_result = StrategyPipelineResult(PipelineStatus.COMPLETE, None)

        class StaticMarketIntelligence:
            def __init__(self):
                self.proposal = proposal

            def run(self, _candidates, *, as_of):
                if len(_candidates) != 1:
                    raise AssertionError("test request should contain one candidate")
                self.result = MarketIntelligenceResult(
                    as_of=as_of,
                    generated_at=self.proposal.generated_at,
                    proposals=(self.proposal,),
                    assessments=(),
                    submission_contexts=(
                        ProposalSubmissionContext(self.proposal, pipeline_result),
                    ),
                )
                return self.result

        intelligence = StaticMarketIntelligence()
        context.market_intelligence = intelligence
        context.research_pipeline = Mock()
        prior_order = ManagedOrder(
            client_order_id="pending",
            broker_order_id="paper-order",
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=None,
            timestamp=generated_at,
            strategy="orb_vwap",
            signal_id=proposal.signal_id,
            risk_decision_id=None,
            status=OrderStatus.ACCEPTED,
        )
        context.paper.order_manager = SimpleNamespace(
            get=Mock(return_value=prior_order))
        context.research_pipeline.submit_decision.return_value = SimpleNamespace(
            order=prior_order, risk={}, duplicate=False)
        client = TestClient(create_app(context))
        body = {
            "instrument_ids": [instrument.instrument_id],
            "as_of": as_of.isoformat(),
        }

        self.assertEqual(
            client.post("/intelligence/opportunities", json=body).status_code,
            401,
        )
        headers = {"Authorization": f"Bearer {TOKEN}"}
        ranked = client.post(
            "/intelligence/opportunities", headers=headers, json=body)
        self.assertEqual(ranked.status_code, 200, ranked.text)

        invalid_quantity = client.post(
            f"/intelligence/proposals/{proposal.proposal_id}/submit",
            headers=headers,
            json={"operator": "test-operator", "quantity": 0},
        )
        self.assertEqual(invalid_quantity.status_code, 422)
        submitted = client.post(
            f"/intelligence/proposals/{proposal.proposal_id}/submit",
            headers=headers,
            json={
                "operator": "test-operator",
                "sizing_mode": "MANUAL_OVERRIDE",
                "quantity": 1,
            },
        )

        self.assertEqual(submitted.status_code, 503, submitted.text)
        context.research_pipeline.submit_decision.assert_not_called()
        replay = client.post(
            f"/intelligence/proposals/{proposal.proposal_id}/submit",
            headers=headers,
            json={
                "operator": "test-operator",
                "sizing_mode": "MANUAL_OVERRIDE",
                "quantity": 1,
            },
        )
        self.assertEqual(replay.status_code, 503, replay.text)
        context.research_pipeline.submit_decision.assert_not_called()

        stale_as_of = datetime.now(timezone.utc) - timedelta(minutes=5)
        intelligence.proposal = replace(
            proposal,
            proposal_id="stale-proposal-id",
            as_of=stale_as_of,
            generated_at=stale_as_of + timedelta(seconds=1),
            regime=replace(proposal.regime, assessed_at=stale_as_of),
            strategy_selection=replace(
                proposal.strategy_selection,
                as_of=stale_as_of,
                generated_at=stale_as_of + timedelta(seconds=1),
            ),
        )
        stale_ranked = client.post(
            "/intelligence/opportunities",
            headers=headers,
            json={
                "instrument_ids": [instrument.instrument_id],
                "as_of": stale_as_of.isoformat(),
            },
        )
        self.assertEqual(stale_ranked.status_code, 200, stale_ranked.text)
        stale = client.post(
            "/intelligence/proposals/stale-proposal-id/submit",
            headers=headers,
            json={
                "operator": "test-operator",
                "sizing_mode": "MANUAL_OVERRIDE",
                "quantity": 1,
            },
        )
        self.assertEqual(stale.status_code, 503)


    def test_proposal_submission_rejects_stale_or_unknown_context(self):
        context, provider = make_context()
        self.addCleanup(provider.close)
        self.addCleanup(context.store.db.close)
        context.settings.max_market_data_age_seconds = 1
        context.market_intelligence = SimpleNamespace()
        context.research_pipeline = Mock()
        client = TestClient(create_app(context))
        response = client.post(
            "/intelligence/proposals/missing/submit",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "operator": "test-operator",
                "sizing_mode": "MANUAL_OVERRIDE",
                "quantity": 1,
            },
        )
        self.assertEqual(response.status_code, 503)

    def test_persisted_proposal_endpoint_delegates_to_paper_lifecycle_service(self):
        context, provider = make_context(configured=False)
        self.addCleanup(provider.close)
        self.addCleanup(context.store.db.close)
        executor = Mock()
        executor.submit.return_value = {
            "proposal_id": "persisted-proposal",
            "execution": "PAPER_ORDER_CREATED",
            "duplicate": False,
        }
        context.paper_proposal_execution = executor
        context.research_pipeline = Mock()
        response = TestClient(create_app(context)).post(
            "/intelligence/proposals/persisted-proposal/submit",
            headers={"Authorization": "Bearer " + TOKEN},
            json={
                "operator": "test-operator",
                "sizing_mode": "MANUAL_OVERRIDE",
                "quantity": 5,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["trading_mode"], "PAPER")
        executor.submit.assert_called_once_with(
            "persisted-proposal",
            operator="test-operator",
            sizing_mode="MANUAL_OVERRIDE",
            quantity=5,
        )
        context.research_pipeline.submit_decision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
