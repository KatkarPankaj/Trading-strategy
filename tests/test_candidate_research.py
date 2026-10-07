from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.ai.candidate_assessment import (
    CandidateAssessmentError,
    CandidateAssessmentService,
)
from stockmarket.core.candidate_research import (
    CandidateResearchService,
    CandidateResearchSettings,
    EvidenceStatus,
    ResearchEvidenceRecord,
    EvidenceQuality,
)
from stockmarket.core.data import DataPolicy, MockProvider, ResilientProvider
from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.strategies import OrbVwapStrategy
from stockmarket.news.models import (
    MarketImpact,
    NewsEvent,
    NewsEventType,
    NewsSentiment,
)
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.research import ResearchObservation
from stockmarket.core.security import Secret


AS_OF = datetime(2026, 10, 8, 20, 1, tzinfo=timezone.utc)
TOKEN = "candidate-research-test-token"


def make_fixture():
    instrument = default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
    index = pd.date_range(
        end=pd.Timestamp("2026-10-08 16:00", tz="America/New_York"),
        periods=45, freq="D")
    close = [100 + number * 0.25 for number in range(len(index))]
    bars = pd.DataFrame({
        "open": close,
        "high": [value + 0.5 for value in close],
        "low": [value - 0.5 for value in close],
        "close": close,
        "volume": [1000 + number * 10 for number in range(len(index))],
    }, index=index)
    raw = MockProvider(
        {instrument.instrument_id: instrument},
        {instrument.instrument_id: bars},
        clock=lambda: AS_OF,
    )
    market_data = ResilientProvider(
        raw, policy=DataPolicy(), clock=lambda: AS_OF)
    db = SQLiteDatabase()
    migrate(db)
    store = Store(db)
    db.execute(
        """INSERT INTO scanner_runs
           (scan_id, universe_id, markets, mode, started_at, completed_at, status,
            requested_count, evaluated_count, accepted_count, rejected_count,
            failed_count, failure_summary, payload, as_of)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("scan-test", "US_ALL", '["US"]', "RESEARCH",
         AS_OF.isoformat(), AS_OF.isoformat(), "COMPLETE",
         1, 1, 1, 0, 0, "[]", "{}",
         (AS_OF - timedelta(hours=2)).isoformat()),
    )
    db.execute(
        """INSERT INTO scanner_candidates
           (scan_id, instrument_id, accepted, selected, score, data_timestamp,
            quality_status, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("scan-test", instrument.instrument_id, 1, 1, 0.9,
         AS_OF.isoformat(), "VALID", "{}"),
    )
    db.execute(
         """INSERT INTO scanner_candidates
            (scan_id, instrument_id, accepted, selected, score, data_timestamp,
             quality_status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
         ("scan-test", "XNAS:MSFT", 1, 0, 0.8,
          AS_OF.isoformat(), "VALID", "{}"),
    )
    service = CandidateResearchService(
        market_data,
        store.scanner_runs,
        store.research_runs,
        {instrument.instrument_id: instrument},
        settings=CandidateResearchSettings(cache_ttl_seconds=0),
        clock=lambda: AS_OF,
    )
    return instrument, market_data, store, service


class StaticNewsProvider:
    name = "test-news"

    def __init__(self):
        self.calls = 0

    def get_news(self, query):
        self.calls += 1
        return (NewsEvent(
            timestamp=AS_OF - timedelta(hours=2),
            source="provider-newsroom",
            headline="Company reports quarterly results",
            event_type=NewsEventType.EARNINGS,
            sentiment=NewsSentiment.UNKNOWN,
            sentiment_confidence=0,
            market_impact=MarketImpact.UNKNOWN,
            relevance=0.8,
            symbol="AAPL",
            affected_market="US",
        ),)


class StaticObservationProvider:
    name = "test-fundamentals"

    def __init__(self):
        self.calls = 0

    def get_observation(self, instrument, *, component, as_of, max_age):
        self.calls += 1
        return ResearchObservation(
            instrument_id=instrument.instrument_id,
            market=instrument.market,
            component=component,
            subject="Reported EPS",
            content="Reported EPS was 2.10 USD.",
            observed_at=as_of - timedelta(minutes=5),
            source=self.name,
            reference="https://example.invalid/earnings",
        )


class BrokenNewsProvider:
    name = "broken-news"

    def get_news(self, _query):
        raise RuntimeError("provider unavailable")


class AssessmentProvider:
    name = "assessment-test-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.users = []

    def complete(self, _system, user):
        self.users.append(user)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


def assessment_response(**overrides):
    value = {
        "instrument_id": "XNAS:AAPL",
        "snapshot_id": "snapshot-test",
        "directional_bias": "BULLISH",
        "confidence": 0.72,
        "opportunity_score": 68.0,
        "risk_flags": ["Historical provider revisions are possible."],
        "key_evidence": [],
        "invalidating_conditions": ["Momentum turns negative."],
        "explanation": "The cited snapshot contains supportive technical and regime evidence.",
    }
    value.update(overrides)
    return value


def strategy_response(**overrides):
    value = {
        "summary": "The registered strategy is suitable for further deterministic evaluation.",
        "ranked_strategies": [{
            "strategy": "orb_vwap",
            "confidence": 0.71,
            "rationale": "Its registered rules can evaluate the supplied intraday context.",
        }],
        "risks": ["Research ranking is not a trade authorization."],
        "data_gaps": ["Macro and sentiment evidence are unavailable."],
    }
    value.update(overrides)
    return value


class CandidateResearchTests(unittest.TestCase):
    def setUp(self):
        self.instrument, self.market_data, self.store, self.service = make_fixture()
        self.addCleanup(self.market_data.close)
        self.addCleanup(self.store.db.close)

    def test_run_builds_timestamped_explainable_snapshot_and_persists_evidence(self):
        run, snapshots = self.service.run_scan("scan-test")

        self.assertEqual(run.status, "PARTIAL")
        self.assertEqual(run.completed_count, 1)
        snapshot, = snapshots
        self.assertEqual(snapshot.instrument_id, self.instrument.instrument_id)
        self.assertEqual(snapshot.scanner_rank, 1)
        self.assertEqual(snapshot.scanner_score, 0.9)
        self.assertIn("momentum", {item.component for item in snapshot.scores})
        self.assertIn("volume", {item.component for item in snapshot.scores})
        self.assertIn("regime", {item.component for item in snapshot.scores})
        self.assertTrue(all(
            item.observed_at is None or item.observed_at <= snapshot.as_of
            for item in snapshot.evidence))
        statuses = {item.component: item.status for item in snapshot.components}
        self.assertEqual(statuses["macro"], EvidenceStatus.MISSING)
        self.assertEqual(statuses["sentiment"], EvidenceStatus.MISSING)
        self.assertEqual(
            self.store.research_runs.get_snapshot(snapshot.snapshot_id)["payload"]["status"],
            "PARTIAL")
        self.assertGreaterEqual(
            len(self.store.research_runs.evidence(snapshot.snapshot_id)), 3)

    def test_future_as_of_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not be in the future"):
            self.service.run_scan("scan-test", as_of=AS_OF + timedelta(seconds=1))

    def test_research_as_of_cannot_precede_scanner_ranking_time(self):
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            self.service.run_scan("scan-test", as_of=AS_OF - timedelta(hours=3))

    def test_paper_scanner_run_is_not_accepted_as_research_input(self):
        self.store.db.execute(
            "UPDATE scanner_runs SET mode = 'PAPER' WHERE scan_id = 'scan-test'")
        with self.assertRaisesRegex(ValueError, "requires a RESEARCH scanner run"):
            self.service.run_scan("scan-test")

    def test_evidence_rejects_post_as_of_observation(self):
        with self.assertRaisesRegex(ValueError, "after as_of"):
            ResearchEvidenceRecord(
                evidence_id="evidence-1",
                instrument_id=self.instrument.instrument_id,
                component="news",
                as_of=AS_OF,
                observed_at=AS_OF + timedelta(seconds=1),
                retrieved_at=AS_OF + timedelta(seconds=2),
                source="news-provider",
                quality=EvidenceQuality.VALID,
                payload={"headline": "future"},
                provenance={},
            )

    def test_news_is_source_attributed_without_ai_scoring(self):
        provider = StaticNewsProvider()
        service = CandidateResearchService(
            self.market_data,
            self.store.scanner_runs,
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            news_provider=provider,
            settings=CandidateResearchSettings(cache_ttl_seconds=0),
            clock=lambda: AS_OF,
        )

        _, snapshots = service.run_scan("scan-test")

        news, = [item for item in snapshots[0].evidence if item.component == "news"]
        self.assertEqual(news.source, "provider-newsroom")
        self.assertEqual(news.payload["headline"], "Company reports quarterly results")
        self.assertEqual(news.provenance["provider"], "test-news")
        self.assertEqual(provider.calls, 1)

    def test_historical_news_requires_an_archive_capable_provider(self):
        provider = StaticNewsProvider()
        service = CandidateResearchService(
            self.market_data,
            self.store.scanner_runs,
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            news_provider=provider,
            settings=CandidateResearchSettings(cache_ttl_seconds=0),
            clock=lambda: AS_OF,
        )

        _, snapshots = service.run_scan(
            "scan-test", as_of=AS_OF - timedelta(hours=1))

        news = next(item for item in snapshots[0].components if item.component == "news")
        self.assertEqual(news.status, EvidenceStatus.UNAVAILABLE)
        self.assertIn("HISTORICAL_NEWS_ARCHIVE_REQUIRED", news.reasons)
        self.assertEqual(provider.calls, 0)

    def test_current_fundamental_facts_are_preserved_without_ai_interpretation(self):
        provider = StaticObservationProvider()
        service = CandidateResearchService(
            self.market_data,
            self.store.scanner_runs,
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            fundamental_provider=provider,
            settings=CandidateResearchSettings(cache_ttl_seconds=0),
            clock=lambda: AS_OF,
        )

        _, snapshots = service.run_scan("scan-test")

        facts, = [
            item for item in snapshots[0].evidence
            if item.component == "fundamental"
        ]
        self.assertIn("Reported EPS", facts.payload["content"])
        self.assertEqual(facts.source, "test-fundamentals")
        self.assertEqual(provider.calls, 1)

    def test_provider_failure_is_explicit_and_does_not_stop_other_components(self):
        service = CandidateResearchService(
            self.market_data,
            self.store.scanner_runs,
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            news_provider=BrokenNewsProvider(),
            settings=CandidateResearchSettings(cache_ttl_seconds=0),
            clock=lambda: AS_OF,
        )

        run, snapshots = service.run_scan("scan-test")

        self.assertEqual(run.status, "PARTIAL")
        news = next(item for item in snapshots[0].components if item.component == "news")
        self.assertEqual(news.status, EvidenceStatus.UNAVAILABLE)
        self.assertTrue(any(item.component == "regime"
                            and item.status == EvidenceStatus.AVAILABLE
                            for item in snapshots[0].components))

    def test_api_exposes_run_and_snapshot_for_manual_inspection(self):
        context = ApiContext(
            settings=SimpleNamespace(),
            paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None,
            store=self.store,
            health=None,
            api_token=Secret(TOKEN),
            instruments={self.instrument.instrument_id: self.instrument},
            candidate_research=self.service,
        )
        client = TestClient(create_app(context))
        self.addCleanup(client.close)
        headers = {"Authorization": f"Bearer {TOKEN}"}

        response = client.post(
            "/research/candidates",
            headers=headers,
            json={"scan_id": "scan-test"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["research_only"])
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        run_id = result["run"]["run_id"]
        snapshot_id = result["snapshots"][0]["snapshot_id"]
        self.assertEqual(
            client.get(f"/research/runs/{run_id}", headers=headers).status_code, 200)
        snapshot = client.get(
            f"/research/snapshots/{snapshot_id}", headers=headers)
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertGreaterEqual(len(snapshot.json()["evidence"]), 3)

    def test_ai_assessment_ranks_and_persists_without_creating_execution_records(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        provider = AssessmentProvider([
            assessment_response(
                instrument_id=snapshot.instrument_id,
                snapshot_id=snapshot.snapshot_id,
                key_evidence=[self.store.research_runs.evidence(
                    snapshot.snapshot_id)[0]["evidence_id"]],
            ),
            strategy_response(),
        ])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF + timedelta(seconds=1)),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            model_version="test-model-v1",
            clock=lambda: AS_OF + timedelta(seconds=2),
        )
        opportunity = service.assess(snapshot.snapshot_id)

        self.assertEqual(
            opportunity.state.value, "STRATEGY_SELECTED",
            opportunity.assessment.error)
        self.assertEqual(opportunity.assessment.status.value, "COMPLETE")
        self.assertEqual(opportunity.assessment.recommended_strategy, "orb_vwap")
        self.assertEqual(opportunity.assessment.model_version, "test-model-v1")
        self.assertEqual(opportunity.assessment.input_context.as_of, AS_OF)
        self.assertEqual(opportunity.lifecycle[-1], "STRATEGY_SELECTED")
        self.assertIsNotNone(opportunity.ranking)
        self.assertGreaterEqual(opportunity.ranking.score, 0)
        self.assertLessEqual(opportunity.ranking.score, 100)
        self.assertIn("not a probability", opportunity.ranking.explanation)
        self.assertEqual(
            service.rank(opportunity.assessment).score,
            opportunity.ranking.score,
        )
        persisted = self.store.research_runs.get_assessment(
            opportunity.assessment.assessment_id)
        self.assertEqual(persisted["payload"]["snapshot_id"], snapshot.snapshot_id)
        self.assertEqual(persisted["opportunity"]["state"], "STRATEGY_SELECTED")
        self.assertEqual(self.store.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
        self.assertEqual(self.store.db.query("SELECT COUNT(*) AS n FROM signals")[0]["n"], 0)
        self.assertEqual(len(provider.users), 2)
        self.assertIn(snapshot.as_of.isoformat(), provider.users[0])

    def test_invalid_evidence_citation_fails_closed_and_is_persisted(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        provider = AssessmentProvider([
            assessment_response(
                instrument_id=snapshot.instrument_id,
                snapshot_id=snapshot.snapshot_id,
                key_evidence=["not-in-this-snapshot"],
            ),
        ])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )

        opportunity = service.assess(snapshot.snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(
            opportunity.assessment.status.value, "AI_INVALID",
            opportunity.assessment.error)
        self.assertIsNone(opportunity.assessment.recommended_strategy)
        self.assertIsNone(opportunity.ranking)
        self.assertIsNotNone(self.store.research_runs.get_assessment(
            opportunity.assessment.assessment_id))
        self.assertEqual(len(provider.users), 1)

    def test_unregistered_strategy_is_rejected(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        provider = AssessmentProvider([
            assessment_response(
                instrument_id=snapshot.instrument_id,
                snapshot_id=snapshot.snapshot_id,
            ),
            strategy_response(ranked_strategies=[{
                "strategy": "invented",
                "confidence": 0.99,
                "rationale": "Not registered.",
            }]),
        ])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )

        opportunity = service.assess(snapshot.snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(
            opportunity.assessment.status.value, "NO_VALID_STRATEGY",
            opportunity.assessment.error)
        self.assertIsNone(opportunity.assessment.recommended_strategy)

    def test_missing_critical_evidence_cannot_be_overridden_by_ai(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        stored = self.store.research_runs.get_snapshot(snapshot.snapshot_id)
        stored["payload"]["components"] = [
            {
                **item,
                "status": "REJECTED",
            } if item["component"] == "technical" else item
            for item in stored["payload"]["components"]
        ]
        self.store.db.execute(
            "UPDATE research_snapshots SET payload = ? WHERE snapshot_id = ?",
            (json.dumps(stored["payload"]), snapshot.snapshot_id),
        )
        provider = AssessmentProvider([
            assessment_response(
                instrument_id=snapshot.instrument_id,
                snapshot_id=snapshot.snapshot_id,
                directional_bias="INSUFFICIENT_EVIDENCE",
            ),
        ])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )

        opportunity = service.assess(snapshot.snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(opportunity.assessment.status.value, "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(opportunity.assessment.recommended_strategy)
        self.assertEqual(len(provider.users), 1)

    def test_future_timestamp_in_persisted_technical_score_is_rejected_before_ai(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        stored = self.store.research_runs.get_snapshot(snapshot.snapshot_id)
        stored["payload"]["scores"][0]["observed_at"] = (
            AS_OF + timedelta(seconds=1)).isoformat()
        self.store.db.execute(
            "UPDATE research_snapshots SET payload = ? WHERE snapshot_id = ?",
            (json.dumps(stored["payload"]), snapshot.snapshot_id),
        )
        provider = AssessmentProvider([])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )

        with self.assertRaisesRegex(
                CandidateAssessmentError, "observed after snapshot as_of"):
            service.assess(snapshot.snapshot_id)
        self.assertEqual(provider.users, [])

    def test_unavailable_ai_is_explicit_and_does_not_select_strategy(self):
        _, snapshots = self.service.run_scan("scan-test")
        provider = AssessmentProvider([RuntimeError("provider offline")])
        service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )

        opportunity = service.assess(snapshots[0].snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(opportunity.assessment.status.value, "AI_UNAVAILABLE")
        self.assertIsNone(opportunity.assessment.recommended_strategy)
        self.assertIsNone(opportunity.ranking)
        self.assertEqual(len(provider.users), 1)

    def test_authenticated_api_assessment_does_not_submit(self):
        _, snapshots = self.service.run_scan("scan-test")
        snapshot = snapshots[0]
        provider = AssessmentProvider([
            assessment_response(
                instrument_id=snapshot.instrument_id,
                snapshot_id=snapshot.snapshot_id,
            ),
            strategy_response(),
        ])
        assessment_service = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF,
        )
        context = ApiContext(
            settings=SimpleNamespace(),
            paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None,
            store=self.store,
            health=None,
            api_token=Secret(TOKEN),
            instruments={self.instrument.instrument_id: self.instrument},
            candidate_assessment=assessment_service,
        )
        client = TestClient(create_app(context))
        self.addCleanup(client.close)

        unauthorized = client.post(f"/research/assessments/{snapshot.snapshot_id}")
        self.assertEqual(unauthorized.status_code, 401)
        response = client.post(
            f"/research/assessments/{snapshot.snapshot_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["research_only"])
        self.assertEqual(response.json()["execution"], "NOT_SUBMITTED")
        self.assertEqual(response.json()["risk_status"], "NOT_EVALUATED")
        self.assertEqual(response.json()["opportunity"]["state"], "STRATEGY_SELECTED")
        assessment_id = response.json()["opportunity"]["assessment"]["assessment_id"]
        self.assertEqual(client.get(
            f"/research/assessments/{assessment_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code, 200)
        self.assertEqual(client.get(
            "/research/opportunities",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code, 200)


if __name__ == "__main__":
    unittest.main()
