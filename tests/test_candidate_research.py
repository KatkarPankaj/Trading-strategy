from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
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


if __name__ == "__main__":
    unittest.main()
