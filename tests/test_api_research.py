from datetime import time, timedelta, timezone
from types import SimpleNamespace
import json
import unittest

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.core import MarketSession, OrbVwapStrategy
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.data import DataPolicy, MockProvider, ResilientProvider
from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.security import Secret
from stockmarket.core.strategy_pipeline import StrategyResearchPipeline


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
    context = ApiContext(
        settings=SimpleNamespace(),
        paper=SimpleNamespace(mode=TradingMode.PAPER),
        live=None,
        store=None,
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


if __name__ == "__main__":
    unittest.main()
