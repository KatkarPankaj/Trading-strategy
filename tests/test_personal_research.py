from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.api.bootstrap import build_context
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.ai.candidate_assessment import CandidateAssessmentService
from stockmarket.core.candidate_research import CandidateResearchService, CandidateResearchSettings
from stockmarket.core.data import DataPolicy, ResilientProvider
from stockmarket.core.data.provider import DataUnavailable, Quote
from stockmarket.core.executors import TradingMode
from stockmarket.core.instrument_master import bootstrap_instruments
from stockmarket.core.markets import default_markets
from stockmarket.core.market_session import MarketSession
from stockmarket.core.models import AssetClass
from stockmarket.core.personal_research import PersonalResearchService
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.scanner import MarketScanner, RegistryUniverseProvider
from stockmarket.core.security import Secret
from stockmarket.core.settings import ConfigurationError, load_settings
from stockmarket.core.strategies import OrbVwapStrategy

from test_candidate_research import assessment_response, strategy_response


AS_OF = datetime(2026, 10, 8, 16, tzinfo=timezone.utc)
TOKEN = "personal-research-test-token"


def make_store():
    db = SQLiteDatabase()
    migrate(db)
    return Store(db)


class ResearchData:
    name = "fixture"
    research_only = True

    def __init__(self, breakout=True):
        daily_index = pd.date_range(
            end="2026-10-07 16:00", periods=45, freq="B", tz="America/New_York")
        daily_close = [95 + index * 0.1 for index in range(45)]
        intraday_index = pd.date_range(
            start="2026-10-08 09:30", periods=31, freq="5min", tz="America/New_York")
        intraday_close = [100.0] * 29 + [102.0 if breakout else 100.0, 150.0]
        self.daily = self.frame(daily_index, daily_close, [10000.0] * 45)
        self.intraday = self.frame(intraday_index, intraday_close, [1000.0] * 29 + [10000.0, 100000.0])
        self.failure = False
        self.future = False
        self.stale = False
        self.empty = False

    @staticmethod
    def frame(index, close, volume):
        return pd.DataFrame({
            "open": close, "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close], "close": close, "volume": volume,
        }, index=index)

    def get_ohlcv(self, instrument, interval, start, end):
        if self.failure:
            raise DataUnavailable("fixture provider unavailable")
        frame = self.daily if interval == "1d" else self.intraday
        if self.empty:
            return frame.iloc[0:0].copy()
        if self.stale and interval != "1d":
            frame = frame.iloc[:-4]
        if self.future and interval != "1d":
            return frame.rename(index=lambda at: at + timedelta(hours=1)).copy()
        return frame.loc[(frame.index >= start) & (frame.index <= end)].copy()

    def get_quote(self, instrument):
        if self.failure:
            raise DataUnavailable("fixture quote unavailable")
        return Quote(instrument.instrument_id, 102.0, AS_OF, self.name)


class ObservedStrategy(OrbVwapStrategy):
    def __init__(self):
        super().__init__()
        self.received = []

    def evaluate(self, instrument, bars, session, *, as_of):
        self.received.append((bars.copy(), as_of))
        return super().evaluate(instrument, bars, session, as_of=as_of)


class AdvisoryAI:
    name = "test-only-advisory"

    def __init__(self, bias="BULLISH"):
        self.bias = bias
        self.users = []

    def complete(self, system, user):
        self.users.append(user)
        if len(self.users) % 2 == 0:
            return json.dumps(strategy_response())
        payload = json.loads(user.split("<untrusted>\n", 1)[1].split("\n</untrusted>", 1)[0])
        return json.dumps(assessment_response(
            instrument_id=payload["instrument_id"], snapshot_id=payload["snapshot_id"],
            directional_bias=self.bias,
            key_evidence=[item["evidence_id"] for item in payload["evidence"] if item["component"] == "strategy"],
        ))


class InstrumentMasterTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.db.close)
        self.markets = default_markets()
        self.env = {"INSTRUMENT_BOOTSTRAP": "development", "PERSONAL_RESEARCH": "true"}

    def test_bootstrap_valid_canonical_records_and_preserves_user_metadata_on_restart(self):
        report = bootstrap_instruments(self.store, self.markets, ("US", "IN"), self.env)
        self.assertEqual(report["inserted"], 16)
        rows = self.store.instruments.list()
        self.assertEqual(len({row["instrument_id"] for row in rows}), 16)
        india = self.store.instruments.get("XNSE:RELIANCE")
        self.assertEqual((india["currency"], india["timezone"]), ("INR", "Asia/Kolkata"))
        verified = self.markets.get("IN").instrument(
            "RELIANCE", mic="XNSE", asset_class=AssetClass.EQUITY,
            tick_size=0.01, tradable=False, name="Operator's existing metadata")
        self.store.instruments.save(verified)
        report = bootstrap_instruments(self.store, self.markets, ("IN",), self.env)
        self.assertEqual((report["inserted"], report["preserved"]), (0, 6))
        self.assertFalse(json.loads(self.store.instruments.get("XNSE:RELIANCE")["payload"])["tradable"])

    def test_empty_duplicate_invalid_and_file_failure_are_explicit_and_atomic(self):
        row = dict(symbol="AAPL", market="US", mic="XNAS", asset_class="EQUITY", tick_size=0.01)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "master.json"
            env = {"INSTRUMENT_MASTER_FILE": str(path)}
            with self.assertRaises(FileNotFoundError):
                bootstrap_instruments(self.store, self.markets, ("US",), env)
            for records in ([], [row, row], [dict(row, tick_size=-1)], [dict(row, unexpected=True)]):
                path.write_text(json.dumps(records), encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    bootstrap_instruments(self.store, self.markets, ("US",), env)
                self.assertEqual(self.store.instruments.list(), [])

    def test_development_requires_recommendation_only_mode(self):
        with self.assertRaisesRegex(ValueError, "PERSONAL_RESEARCH"):
            bootstrap_instruments(self.store, self.markets, ("US",), {"INSTRUMENT_BOOTSTRAP": "development"})
        report = bootstrap_instruments(self.store, self.markets, ("US",), {})
        self.assertIn("NO_MASTER_CONFIGURED", report["reason"])


class PersonalResearchTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.db.close)
        self.markets = default_markets()
        instrument = self.markets.get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.instruments = {instrument.instrument_id: instrument}
        self.store.instruments.save(instrument)
        self.raw = ResearchData()
        self.data = ResilientProvider(self.raw, policy=DataPolicy(), clock=lambda: AS_OF)
        self.addCleanup(self.data.close)
        self.strategy = ObservedStrategy()
        self.strategies = {"orb_vwap": self.strategy}
        market = self.markets.get("US")
        self.sessions = {"US": MarketSession(
            timezone=market.timezone, market_open=market.calendar.open_time,
            opening_range_end=time(9, 45), entry_cutoff=time(15),
            square_off=time(15, 55), market_close=market.calendar.close_time)}
        self.scanner = MarketScanner(
            self.data, RegistryUniverseProvider(self.instruments, ("US",)), self.markets,
            repository=self.store.scanner_runs, clock=lambda: AS_OF)
        self.research = CandidateResearchService(
            self.data, self.store.scanner_runs, self.store.research_runs, self.instruments,
            settings=CandidateResearchSettings(cache_ttl_seconds=0), clock=lambda: AS_OF)
        self.service = PersonalResearchService(
            self.store, self.scanner, self.data, self.instruments, self.markets,
            self.strategies, self.sessions, self.research, None, clock=lambda: AS_OF)

    def enable_ai(self, bias="BULLISH"):
        provider = AdvisoryAI(bias)
        self.service.assessment = CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF), self.store.research_runs,
            self.instruments, self.markets, self.strategies,
            model_version="fixture-only-v1", clock=lambda: AS_OF)
        return provider

    def run_research(self, key="test"):
        return self.service.run("US_ALL", key, top_n=1, as_of=AS_OF)

    def assert_no_orders(self):
        self.assertEqual(self.store.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
        self.assertEqual(self.store.db.query("SELECT COUNT(*) AS n FROM fills")[0]["n"], 0)

    def test_strategy_and_research_work_without_ai_final_recommendation_withheld(self):
        result = self.run_research()
        row = result["recommendations"][0]
        self.assertEqual(row["strategy_signal"]["side"], "BUY")
        self.assertEqual((row["direction"], row["ai_status"]), ("AVOID", "CONFIGURATION_MISSING"))
        self.assertEqual(row["data_quality"], "PASS")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        bars, as_of = self.strategy.received[0]
        self.assertTrue(all(bars.index + timedelta(minutes=5) <= as_of))
        self.assertEqual(bars.iloc[-1]["close"], 102.0)
        self.assertTrue(any(item["component"] == "strategy" for item in row["evidence"]["evidence"]))
        self.assert_no_orders()

    def test_full_deterministic_strategy_snapshot_ai_ranking_persistence_without_orders(self):
        provider = self.enable_ai()
        result = self.run_research()
        row = result["recommendations"][0]
        self.assertEqual(row["direction"], "BUY", row["reason"])
        self.assertEqual(row["ai_status"], "COMPLETE", row.get("ai_assessment"))
        self.assertTrue(0 < row["score"] <= 100)
        self.assertEqual(row["ai_assessment"]["model_version"], "fixture-only-v1")
        self.assertIn("deterministic_strategy", provider.users[0])
        self.assertEqual(self.store.personal_research.get(result["run_id"]), result)
        self.assertEqual(len(provider.users), 2)
        self.assert_no_orders()

    def test_ai_cannot_override_hold_or_reverse_buy(self):
        self.raw.intraday = ResearchData(breakout=False).intraday
        provider = self.enable_ai()
        result = self.run_research()
        row = result["recommendations"][0]
        self.assertEqual(row["strategy_signal"]["side"], "HOLD")
        self.assertEqual(row["direction"], "AVOID")
        self.assertEqual(len(provider.users), 2)
        self.assert_no_orders()

    def test_disagreeing_ai_withholds_buy(self):
        self.enable_ai("BEARISH")
        row = self.run_research()["recommendations"][0]
        self.assertEqual((row["strategy_signal"]["side"], row["direction"]), ("BUY", "AVOID"))
        self.assert_no_orders()

    def test_future_and_stale_bars_do_not_reach_strategy(self):
        for mode in ("future", "stale"):
            with self.subTest(mode=mode):
                setattr(self.raw, mode, True)
                result = self.run_research(mode)
                row = result["recommendations"][0]
                self.assertEqual(row["direction"], "AVOID")
                self.assertIsNone(row["strategy_signal"])
                self.assertIn(row["data_quality"], ("INVALID", "STALE"))
                setattr(self.raw, mode, False)
        self.assertEqual(self.strategy.received, [])
        self.assert_no_orders()

    def test_invalid_instrument_missing_provider_empty_data_and_future_request(self):
        with self.assertRaises(KeyError):
            self.service.diagnostics("XNAS:UNKNOWN")
        with self.assertRaisesRegex(ValueError, "future"):
            self.service.run("US_ALL", "future-time", as_of=AS_OF + timedelta(seconds=1))
        self.raw.failure = True
        diagnostics = self.service.diagnostics("XNAS:AAPL", AS_OF)
        self.assertEqual(diagnostics["ohlcv"]["status"], "MISSING")
        self.assertIn("PROVIDER_UNAVAILABLE", diagnostics["quote"]["reason"])
        self.raw.failure = False
        self.raw.empty = True
        result = self.run_research()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("NO_ELIGIBLE_CANDIDATES", result["failure"])
        self.assert_no_orders()

    def test_duplicate_replay_after_restart_and_conflict(self):
        result = self.run_research()
        restarted = PersonalResearchService(
            self.store, self.scanner, self.data, self.instruments, self.markets,
            self.strategies, self.sessions, self.research, None,
            clock=lambda: AS_OF + timedelta(days=1))
        self.assertEqual(restarted.run("US_ALL", "test", top_n=1), result)
        self.assertEqual(len(self.strategy.received), 1)
        with self.assertRaisesRegex(ValueError, "different request"):
            restarted.run("US_ALL", "test", top_n=2)
        self.assert_no_orders()

    def test_uncovered_india_calendar_is_unsupported_not_a_successful_empty_scan(self):
        instrument = self.markets.get("IN").instrument(
            "SBIN", mic="XNSE", asset_class=AssetClass.EQUITY, tick_size=0.05)
        instruments = {instrument.instrument_id: instrument}
        scanner = MarketScanner(
            self.data, RegistryUniverseProvider(instruments, ("IN",)), self.markets,
            repository=self.store.scanner_runs, clock=lambda: AS_OF)
        service = PersonalResearchService(
            self.store, scanner, self.data, instruments, self.markets,
            self.strategies, {}, self.research, None, clock=lambda: AS_OF)
        result = service.run("IN_ALL", "unsupported", top_n=1, as_of=AS_OF)
        self.assertEqual(result["status"], "FAILED")
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["ohlcv_status"], "UNSUPPORTED")
        self.assertFalse(diagnostic["calendar_covered"])
        self.assertEqual(diagnostic["provider_status"], "NOT_FETCHED")
        self.assertEqual(self.strategy.received, [])
        self.assert_no_orders()

    def test_failed_run_is_durable_and_interrupted_run_is_not_success(self):
        with patch.object(self.service, "_evaluate", side_effect=OSError("fixture infrastructure error")):
            with self.assertLogs("stockmarket.core.personal_research", level="ERROR"):
                with self.assertRaises(OSError):
                    self.run_research()
        failed = self.store.personal_research.get_by_key("test")
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(self.run_research(), failed)
        self.store.personal_research.finish(dict(failed, status="RUNNING"))
        with self.assertRaisesRegex(ValueError, "interrupted research run"):
            self.run_research()
        self.assert_no_orders()

    def test_api_guard_blocks_execution_and_exposes_persisted_runs(self):
        context = ApiContext(
            settings=SimpleNamespace(markets=("US",)), paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None, store=self.store, health=None, api_token=Secret(TOKEN),
            instruments=self.instruments, markets=self.markets, personal_research=self.service,
            personal_research_mode=True)
        with TestClient(create_app(context)) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            for path in (
                "/paper/orders", "/live/orders", "/paper/positions/manage",
                "/paper/cycles", "/paper/cycles/test/recover",
                "/intelligence/proposals/test/submit", "/orders/test/cancel",
                "/recovery/resume", "/recovery/reconcile", "/kill-switch/reset", "/kill-switch/trigger",
            ):
                self.assertEqual(client.post(path, headers=headers, json={}).status_code, 403, path)
            self.assertEqual(client.post("/research/personal/runs", json={}).status_code, 401)
            response = client.post("/research/personal/runs", headers=headers, json={
                "universe_id": "US_ALL", "idempotency_key": "api-test",
                "top_n": 1, "as_of": AS_OF.isoformat(),
            })
            self.assertEqual(response.status_code, 200, response.text)
            run_id = response.json()["run_id"]
            self.assertEqual(client.get(f"/research/personal/runs/{run_id}", headers=headers).json(), response.json())
            self.assertEqual(client.get("/research/personal/status", headers=headers).json()["ai_status"], "CONFIGURATION_MISSING")
        self.assert_no_orders()


class PersonalBootstrapTests(unittest.TestCase):
    def test_bootstrap_wires_master_sessions_gate_and_disabled_cycle(self):
        store = make_store()
        self.addCleanup(store.db.close)
        env = {
            "APP_ENV": "test", "MARKETS": "US", "BASE_CURRENCY": "USD",
            "API_TOKEN": TOKEN, "DATA_PROVIDER": "mock", "PERSONAL_RESEARCH": "true",
            "INSTRUMENT_BOOTSTRAP": "development",
            "ENTRY_WINDOW_START": "09:45", "ENTRY_WINDOW_END": "15:00",
            "MAX_POSITION_QUANTITY": "20", "MAX_ORDER_NOTIONAL": "25000",
            "RESEARCH_SESSIONS": json.dumps({"US": {
                "opening_range_minutes": 15, "entry_cutoff": "15:00",
                "square_off": "15:55", "late_entry_start": "12:00"}}),
        }
        context = build_context(load_settings(env), env, store=store)
        self.addCleanup(context.market_data.close)
        self.assertEqual(len(context.instruments), 16)
        self.assertIn("PERSONAL_RESEARCH", context.gate.reasons())
        self.assertIsNone(context.autonomous_paper_trading)
        self.assertIsNotNone(context.personal_research)
        self.assertIn("US_LIQUID_DEVELOPMENT", {
            item.universe_id for item in context.scanner.list_universes()})
        with self.assertRaises(ConfigurationError):
            build_context(load_settings(env), dict(env, PERSONAL_RESEARCH="false", INSTRUMENT_BOOTSTRAP="none"), store=store)
