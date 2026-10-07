from __future__ import annotations

import unittest
from types import SimpleNamespace
from datetime import datetime, time, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.core.data.provider import DataUnavailable, Quote
from stockmarket.core.executors import TradingMode
from stockmarket.core.markets import MarketDefinition, MarketRegistry
from stockmarket.core.models import AssetClass, Instrument
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.persistence.repositories import ScannerRunRepository
from stockmarket.core.security import Secret
from stockmarket.core.scanner import (
    CandidateQuality,
    MarketScanner,
    MarketSessionStatus,
    ScanMode,
    ScanStatus,
    ScannerSettings,
    StaticUniverseProvider,
    UniverseDefinition,
    parse_scanner_settings,
    parse_universe_definitions,
)
from stockmarket.core.trading_calendar import TradingCalendar


class FakeMarketData:
    name = "fake-research"
    research_only = True

    def __init__(
        self,
        bars: pd.DataFrame,
        now: datetime,
        failing: set[str] | None = None,
        quote_age: timedelta = timedelta(minutes=1),
    ):
        self.bars = bars
        self.now = now
        self.failing = failing or set()
        self.quote_age = quote_age

    def get_ohlcv(self, instrument, interval, start, end):
        if instrument.instrument_id in self.failing:
            raise DataUnavailable("deterministic provider failure")
        return self.bars.loc[(self.bars.index >= start) & (self.bars.index <= end)].copy()

    def get_quote(self, instrument):
        if instrument.instrument_id in self.failing:
            raise DataUnavailable("deterministic provider failure")
        return Quote(
            instrument.instrument_id, 110.0, self.now - self.quote_age,
            self.name, volume=1500,
        )


def scanner_fixture(
    *,
    failing: set[str] | None = None,
    repository=None,
    as_of=None,
    quote_age=timedelta(minutes=1),
):
    as_of = as_of or datetime(2025, 1, 10, 12, tzinfo=timezone.utc)
    calendar = TradingCalendar("UTC", time(9), time(16), covered_years=None)
    markets = MarketRegistry()
    markets.register(MarketDefinition(
        "TST", "Test market", ("XTST",), "USD", calendar,
        pre_market_open=time(8), post_market_close=time(18),
    ))
    instruments = {
        f"XTST:SYM{number}": Instrument(
            instrument_id=f"XTST:SYM{number}", symbol=f"SYM{number}", exchange="XTST",
            market="TST", asset_class=AssetClass.EQUITY, currency="USD",
            timezone="UTC", tick_size=0.01, mic="XTST",
        )
        for number in range(2)
    }
    dates = pd.date_range("2024-12-02 10:00", periods=25, freq="B", tz="UTC")
    close = [100 + index * 0.4 for index in range(len(dates))]
    volumes = [1000.0] * (len(dates) - 1) + [1500.0]
    bars = pd.DataFrame(
        {
            "open": [value - 0.2 for value in close],
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "close": close,
            "volume": volumes,
        },
        index=dates,
    )
    definition = UniverseDefinition(
        "TST_LIQUID", "Test liquid equities", ("TST",),
        asset_classes=(AssetClass.EQUITY,), minimum_price=10,
        minimum_average_volume=500, minimum_turnover_by_currency={"USD": 50_000},
    )
    provider = StaticUniverseProvider((definition,), instruments)
    scanner = MarketScanner(
        FakeMarketData(bars, as_of, failing, quote_age), provider, markets,
        repository=repository, clock=lambda: as_of,
        settings=ScannerSettings(minimum_history_bars=20, lookback_days=90),
    )
    return scanner, as_of


class MarketScannerTests(unittest.TestCase):
    def test_research_scan_filters_ranks_and_persists_explainable_candidates(self):
        db = SQLiteDatabase()
        migrate(db)
        repo = ScannerRunRepository(db)
        scanner, _ = scanner_fixture(repository=repo)

        result = scanner.scan("TST_LIQUID", ScanMode.RESEARCH, top_n=1)

        self.assertEqual(result.status, ScanStatus.COMPLETE)
        self.assertEqual((result.requested_count, result.evaluated_count), (2, 2))
        self.assertEqual(result.accepted_count, 2)
        top = [candidate for candidate in result.candidates if not candidate.rejection_reasons]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].data_quality, CandidateQuality.VALID)
        self.assertEqual(top[0].market_status, MarketSessionStatus.OPEN)
        self.assertEqual(top[0].currency, "USD")
        self.assertTrue(all(item.passed for item in top[0].filter_results))
        self.assertEqual(
            {name for name, _ in top[0].score_components},
            {"liquidity", "momentum", "volume_expansion",
             "volatility_suitability", "data_quality"},
        )
        stored = repo.get(result.scan_id)
        self.assertEqual(stored["status"], "COMPLETE")
        stored_candidates = repo.candidates(result.scan_id, accepted_only=True)
        self.assertEqual(len(stored_candidates), 2)
        db.close()

    def test_provider_failure_is_reported_as_partial_not_as_a_valid_candidate(self):
        scanner, _ = scanner_fixture(failing={"XTST:SYM1"})

        result = scanner.scan("TST_LIQUID", ScanMode.RESEARCH)

        self.assertEqual(result.status, ScanStatus.PARTIAL)
        self.assertEqual(result.evaluated_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertTrue(any(item.startswith("XTST:SYM1:DataUnavailable")
                            for item in result.failure_summary))

    def test_paper_scan_rejects_stale_quotes(self):
        db = SQLiteDatabase()
        migrate(db)
        repo = ScannerRunRepository(db)
        scanner, _ = scanner_fixture(
            repository=repo, quote_age=timedelta(seconds=61))

        result = scanner.scan("TST_LIQUID", ScanMode.PAPER)

        self.assertEqual(result.status, ScanStatus.FAILED)
        self.assertEqual(result.accepted_count, 0)
        stored = repo.candidates(result.scan_id)
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(
            row["quality_status"] == CandidateQuality.STALE.value for row in stored
        ))
        db.close()

    def test_paper_scan_rejects_non_open_market_and_research_needs_completed_bars(self):
        closed_at = datetime(2025, 1, 10, 20, tzinfo=timezone.utc)
        scanner, _ = scanner_fixture(as_of=closed_at)

        result = scanner.scan("TST_LIQUID", ScanMode.PAPER)

        self.assertEqual(result.accepted_count, 0)
        self.assertTrue(all(
            candidate.market_status is MarketSessionStatus.CLOSED
            for candidate in result.candidates
        ))

    def test_paper_scan_can_generate_preopen_candidates_without_executing_orders(self):
        preopen = datetime(2025, 1, 10, 8, 30, tzinfo=timezone.utc)
        scanner, _ = scanner_fixture(as_of=preopen)

        result = scanner.scan("TST_LIQUID", ScanMode.PAPER)

        self.assertEqual(result.status, ScanStatus.COMPLETE)
        self.assertGreater(result.accepted_count, 0)
        self.assertTrue(all(
            candidate.market_status is MarketSessionStatus.PRE_OPEN
            for candidate in result.candidates
        ))

    def test_inactive_universe_fails_empty_and_unknown_universe_fails(self):
        base, _ = scanner_fixture()
        provider = StaticUniverseProvider(
            (UniverseDefinition("OFF", "Inactive", ("TST",), active=False),),
            {},
        )
        scanner = MarketScanner(
            base._data, provider, base._markets, clock=base._clock,
            settings=ScannerSettings(minimum_history_bars=20),
        )
        result = scanner.scan("OFF", ScanMode.RESEARCH)
        self.assertEqual(result.status, ScanStatus.FAILED)
        self.assertEqual(result.requested_count, 0)
        with self.assertRaises(KeyError):
            scanner.scan("UNKNOWN", ScanMode.RESEARCH)

    def test_authenticated_scanner_api_exposes_universes_runs_and_candidates(self):
        db = SQLiteDatabase()
        migrate(db)
        store = Store(db)
        scanner, _ = scanner_fixture(repository=store.scanner_runs)
        context = ApiContext(
            settings=SimpleNamespace(),
            paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None,
            store=store,
            health=None,
            api_token=Secret("scanner-api-test-token"),
            scanner=scanner,
        )
        headers = {"Authorization": "Bearer scanner-api-test-token"}

        with TestClient(create_app(context)) as client:
            listed = client.get("/universes", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()[0]["universe"]["universe_id"], "TST_LIQUID")
            response = client.post(
                "/scanner/scan",
                headers=headers,
                json={"universe_id": "TST_LIQUID", "mode": "RESEARCH", "top_n": 1},
            )
            self.assertEqual(response.status_code, 200)
            scan_id = response.json()["scan_id"]
            self.assertEqual(
                client.get(f"/scanner/runs/{scan_id}", headers=headers).status_code, 200)
            candidates = client.get(
                "/scanner/candidates",
                headers=headers,
                params={"scan_id": scan_id, "accepted_only": True},
            )
            self.assertEqual(candidates.status_code, 200)
            self.assertEqual(len(candidates.json()), 2)
        db.close()

    def test_configuration_is_data_only_and_resource_bounded(self):
        definitions = parse_universe_definitions(
            '[{"universe_id":"US_ETFS","name":"US ETFs","markets":["US"],'
            '"asset_classes":["ETF"],"minimum_turnover_by_currency":{"USD":1000000}}]'
        )
        self.assertEqual(definitions[0].asset_classes, (AssetClass.ETF,))
        with self.assertRaises(ValueError):
            parse_universe_definitions(
                '[{"universe_id":"BAD","name":"Bad","markets":["US"],'
                '"predicate":"__import__(\\"os\\").system(\\"whoami\\")"}]'
            )
        with self.assertRaises(ValueError):
            parse_scanner_settings('{"max_concurrency":1000000}')


if __name__ == "__main__":
    unittest.main()
