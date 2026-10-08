from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, time, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.api.bootstrap import build_context
from stockmarket.core.data import MockProvider
from stockmarket.core.executors import TradingMode
from stockmarket.core.market_profiles import MarketProfileService, default_research_sessions
from stockmarket.core.markets import MarketRegistry, default_markets
from stockmarket.core.scanner import MarketScanner, RegistryUniverseProvider, StaticUniverseProvider, UniverseDefinition
from stockmarket.core.security import Secret
from stockmarket.core.settings import load_settings
from stockmarket.core.strategies import OrbVwapStrategy
from stockmarket.dashboard import app as dashboard

import test_personal_research as research_fixture
from test_personal_research import TOKEN, make_store, AS_OF


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class MarketProfileTests(unittest.TestCase):
    def setUp(self):
        self.registry = default_markets()
        self.scanner = MarketScanner(
            MockProvider({}), RegistryUniverseProvider({}, self.registry.codes()), self.registry)
        self.service = MarketProfileService(
            self.registry, self.scanner, {"orb_vwap": OrbVwapStrategy()},
            default_research_sessions(self.registry),
            data_provider="mock", default_market="IN", clock=lambda: AS_OF)

    def test_india_us_germany_profiles_and_india_calendar_fail_closed(self):
        expected = {
            "IN": ("INR", "Asia/Kolkata", ("XNSE", "XBOM")),
            "US": ("USD", "America/New_York", ("XNYS", "XNAS", "ARCX")),
            "DE": ("EUR", "Europe/Berlin", ("XETR",)),
        }
        for code, (currency, zone, mics) in expected.items():
            context = self.service.status(code, as_of=at("2026-10-08T10:00:00"))
            self.assertEqual(context["resolved_market"], code)
            self.assertEqual((context["currency"], context["timezone"], context["mics"]), (currency, zone, mics))
            self.assertEqual(context["default_universe_id"], f"{code}_ALL")
            self.assertEqual(context["supported_strategies"], ["orb_vwap"])
            self.assertIsNotNone(context["research_session"])
        india = self.service.status("IN", as_of=at("2026-10-08T05:00:00"))
        self.assertEqual((india["status"], india["session"]), ("UNSUPPORTED_CALENDAR", "UNSUPPORTED_CALENDAR"))
        self.assertEqual(india["research_session"]["entry_start"], "09:45:00")
        germany = self.service.status("DE", as_of=at("2026-10-08T10:00:00"))
        self.assertEqual((germany["status"], germany["session"]), ("OPEN", "REGULAR"))
        self.assertIn("EMPTY_MARKET_UNIVERSE", ";".join(germany["limitations"]))

    def test_auto_active_session_priority_and_configured_default_tie_break(self):
        # Germany regular beats US pre-market, without considering India's wall clock.
        self.assertEqual(self.service.status("AUTO", as_of=at("2026-10-08T10:00:00"))["resolved_market"], "DE")
        self.assertEqual(self.service.status("AUTO", as_of=at("2026-10-08T18:00:00"))["resolved_market"], "US")
        # Both US and Germany regular: configured default first, then stable market code.
        self.service.default_market = "US"
        self.assertEqual(self.service.status(as_of=at("2026-10-08T14:00:00"))["resolved_market"], "US")
        self.service.default_market = "DE"
        self.assertEqual(self.service.status(as_of=at("2026-10-08T14:00:00"))["resolved_market"], "DE")
        self.assertEqual(self.service.status(as_of=at("2026-10-08T06:15:00"))["session"], "PRE_MARKET")
        self.assertEqual(self.service.status(as_of=at("2026-10-08T22:00:00"))["session"], "POST_MARKET")

    def test_auto_falls_back_to_actual_default_status_when_all_closed(self):
        context = self.service.status(as_of=at("2026-10-10T12:00:00"))
        self.assertEqual(context["resolved_market"], "IN")
        self.assertEqual(context["status"], "UNSUPPORTED_CALENDAR")
        self.assertEqual(context["resolution_reason"], "NO_ACTIVE_SESSION_DEFAULT")
        self.service.default_market = "US"
        context = self.service.status(as_of=at("2026-10-10T12:00:00"))
        self.assertEqual(context["status"], "CLOSED")

    def test_manual_selection_never_changes_when_other_market_opens(self):
        for timestamp in ("2026-10-08T10:00:00", "2026-10-08T18:00:00"):
            for code in ("IN", "US", "DE"):
                context = self.service.status(code, as_of=at(timestamp))
                self.assertEqual(context["resolved_market"], code)
                self.assertEqual(context["resolution_reason"], "EXPLICIT_SELECTION")

    def test_dst_mismatch_weeks_use_exchange_timezone(self):
        for value, expected_session, expected_offset in (
            ("2026-03-06T13:45:00", "PRE_MARKET", "-05:00"),
            ("2026-03-09T13:45:00", "REGULAR", "-04:00"),
            ("2026-10-26T13:45:00", "REGULAR", "-04:00"),
            ("2026-11-02T13:45:00", "PRE_MARKET", "-05:00"),
        ):
            context = self.service.status("US", as_of=at(value))
            self.assertEqual(context["session"], expected_session)
            self.assertTrue(context["local_timestamp"].endswith(expected_offset))
        for value, offset in (
            ("2026-03-09T13:45:00", "+01:00"),
            ("2026-03-30T13:45:00", "+02:00"),
            ("2026-10-26T13:45:00", "+01:00"),
        ):
            context = self.service.status("DE", as_of=at(value))
            self.assertTrue(context["local_timestamp"].endswith(offset))
            self.assertEqual(context["session"], "REGULAR")

    def test_holiday_post_market_early_close_and_uncovered_year(self):
        self.assertEqual(self.service.status("US", as_of=at("2026-11-26T16:00:00"))["status"], "HOLIDAY")
        self.assertEqual(self.service.status("US", as_of=at("2026-11-27T18:01:00"))["status"], "POST_MARKET")
        for code in ("IN", "US", "DE"):
            self.assertEqual(self.service.status(code, as_of=at("2027-01-04T15:00:00"))["status"], "UNSUPPORTED_CALENDAR")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.service.status(as_of=datetime(2026, 10, 8))
        self.assertEqual(self.service.status("US", as_of=at("2026-10-08T06:00:00"))["status"], "CLOSED")
        self.assertEqual(self.service.status("US", as_of=at("2027-01-01T01:00:00"))["status"], "CLOSED")
        self.assertEqual(self.service.status("DE", as_of=at("2027-01-01T01:00:00"))["status"], "UNSUPPORTED_CALENDAR")

    def test_germany_without_calendar_is_not_reported_open(self):
        registry = MarketRegistry()
        for code in self.registry.codes():
            definition = self.registry.get(code)
            if code == "DE":
                definition = replace(definition, calendar=replace(definition.calendar, covered_years=frozenset()))
            registry.register(definition)
        self.service.registry = registry
        result = self.service.status("DE", as_of=at("2026-10-08T10:00:00"))
        self.assertEqual(result["status"], "UNSUPPORTED_CALENDAR")
        self.assertFalse(result["calendar_covered"])

    def test_future_registered_market_and_cross_market_universe_filter(self):
        registry = self.registry
        registry.register(replace(registry.get("DE"), code="TST", name="Future test market"))
        sessions = default_research_sessions(registry)
        self.service.sessions = sessions
        self.service.scanner = MarketScanner(
            MockProvider({}), StaticUniverseProvider((
                UniverseDefinition("CROSS", "Cross-country", ("US", "DE")),
                UniverseDefinition("TST_ALL", "Future test universe", ("TST",)),
            ), {}), registry)
        context = self.service.status("TST", as_of=at("2026-10-08T10:00:00"))
        self.assertEqual(context["currency"], "EUR")
        self.assertEqual(context["default_universe_id"], "TST_ALL")
        self.assertEqual(self.service.status("US")["universes"], [])


class MarketSelectionPipelineTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing offline US provider/strategy/research fixture, not its test cases.
        fixture = research_fixture.PersonalResearchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.service = fixture.service

    def test_selected_market_flows_through_persisted_research_without_ai_or_orders(self):
        result = self.service.run(None, "market-us", selected_market="US", top_n=1, as_of=AS_OF)
        context = result["market_context"]
        self.assertEqual((context["resolved_market"], context["currency"], context["timezone"]),
                         ("US", "USD", "America/New_York"))
        self.assertEqual(result["universe_id"], "US_ALL")
        row = result["recommendations"][0]
        self.assertEqual((row["strategy_signal"]["side"], row["direction"]), ("BUY", "AVOID"))
        self.assertEqual(row["ai_status"], "CONFIGURATION_MISSING")
        self.assertEqual(result, self.fixture.store.personal_research.get(result["run_id"]))
        self.fixture.assert_no_orders()

    def test_auto_replay_keeps_original_resolution_and_ai_cannot_create_orders(self):
        self.fixture.enable_ai()
        result = self.service.run(None, "auto-us", selected_market="AUTO", top_n=1, as_of=AS_OF)
        self.assertEqual(result["market_context"]["resolved_market"], "US")
        self.assertEqual(result["recommendations"][0]["direction"], "BUY")
        self.service.clock = lambda: at("2026-10-09T10:00:00")
        with patch.object(self.service.market_profiles, "status") as resolve:
            self.assertEqual(self.service.run(None, "auto-us", selected_market="AUTO", top_n=1), result)
            resolve.assert_not_called()
        with self.assertRaisesRegex(ValueError, "different request"):
            self.service.run(None, "auto-us", selected_market="US", top_n=1)
        self.fixture.assert_no_orders()

    def test_explicit_market_rejects_mismatched_universe_before_scan(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.service.run("US_ALL", "wrong-country", selected_market="DE", as_of=AS_OF)
        self.assertEqual(self.fixture.store.personal_research.recent(), [])
        self.fixture.assert_no_orders()

    def test_api_profiles_authenticated_market_filter_and_ai_status(self):
        context = ApiContext(
            settings=SimpleNamespace(markets=("IN",)), paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None, store=self.fixture.store, health=None, api_token=Secret(TOKEN),
            instruments=self.fixture.instruments, markets=self.fixture.markets,
            personal_research=self.service, personal_research_mode=True)
        with TestClient(create_app(context)) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            self.assertEqual(client.get("/markets/status").status_code, 401)
            response = client.get("/markets/status?selected_market=US", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ai_status"], "CONFIGURATION_MISSING")
            self.assertEqual(len(response.json()["profiles"]), 3)
            self.assertEqual(client.get("/markets/status?selected_market=UNKNOWN", headers=headers).status_code, 404)
            self.assertEqual(len(client.get("/instruments?market=US", headers=headers).json()), 1)
            self.fixture.enable_ai()
            self.assertEqual(client.get("/markets/status", headers=headers).json()["ai_status"], "CONFIGURED")
            response = client.post("/research/personal/runs", headers=headers, json={
                "selected_market": "US", "idempotency_key": "api-market", "top_n": 1,
                "as_of": AS_OF.isoformat()})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["market_context"]["resolved_market"], "US")
            self.assertEqual(client.post("/paper/orders", headers=headers, json={}).status_code, 403)
        self.fixture.assert_no_orders()


class MarketBootstrapTests(unittest.TestCase):
    def env(self):
        return {
            "APP_ENV": "test", "PERSONAL_RESEARCH": "true", "API_TOKEN": TOKEN,
            "INSTRUMENT_BOOTSTRAP": "development", "DATA_PROVIDER": "mock",
            "ENTRY_WINDOW_START": "09:45", "ENTRY_WINDOW_END": "15:00",
            "MAX_POSITION_QUANTITY": "20", "MAX_ORDER_NOTIONAL": "25000",
        }

    def build(self, env):
        store = make_store()
        self.addCleanup(store.db.close)
        context = build_context(load_settings(env), env, store=store)
        self.addCleanup(context.market_data.close)
        return context

    def test_no_market_currency_or_session_env_needed_and_no_execution(self):
        context = self.build(self.env())
        self.assertEqual(len(context.instruments), 16)
        self.assertEqual(set(context.research_sessions), {"US", "IN", "DE"})
        self.assertEqual(context.research_sessions["IN"].effective_entry_start, time(9, 45))
        self.assertEqual(context.research_sessions["DE"].timezone, "Europe/Berlin")
        self.assertIsNone(context.candidate_assessment)
        self.assertIsNone(context.autonomous_paper_trading)
        self.assertTrue(context.gate.halted)
        self.assertEqual(context.store.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)

    def test_ai_configuration_does_not_require_session_env_in_personal_mode(self):
        env = dict(self.env(), MARKETS="IN", BASE_CURRENCY="INR",
                   AI_BASE_URL="https://example.invalid/v1", AI_MODEL="fixture-model",
                   AI_API_KEY="fixture-key-not-a-real-secret")
        context = self.build(env)
        self.assertIsNotNone(context.candidate_assessment)
        self.assertEqual(context.personal_research.market_profiles.default_market, "IN")
        self.assertEqual(context.paper.portfolio.base_currency, "INR")
        self.assertEqual(set(context.research_sessions), {"US", "IN", "DE"})
        self.assertEqual(len(context.instruments), 16)

    def test_explicit_research_session_override_preserves_other_profiles(self):
        env = dict(self.env(), RESEARCH_SESSIONS=(
            '{"US":{"opening_range_minutes":20,"entry_cutoff":"14:00",'
            '"square_off":"15:50","entry_start":"10:00","late_entry_start":"12:00"}}'))
        context = self.build(env)
        self.assertEqual(context.research_sessions["US"].effective_entry_start, time(10))
        self.assertEqual(context.research_sessions["IN"].effective_entry_start, time(9, 45))
        self.assertIn("DE", context.research_sessions)

    def test_empty_germany_and_unsupported_india_never_create_recommendations_or_fills(self):
        context = self.build(self.env())
        service = context.personal_research
        service.clock = lambda: AS_OF
        with patch.object(service.scanner, "_clock", return_value=AS_OF):
            for code in ("DE", "IN"):
                result = service.run(None, f"safe-{code}", selected_market=code, as_of=AS_OF)
                self.assertEqual(result["recommendations"], [])
                self.assertEqual(result["execution"], "NOT_SUBMITTED")
                self.assertEqual(result["market_context"]["resolved_market"], code)
        for table in ("orders", "fills"):
            self.assertEqual(context.store.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"], 0)


class PersonalMarketDashboardTests(unittest.TestCase):
    @patch.object(dashboard, "st")
    def test_selector_renders_backend_context_and_submits_country_not_env_settings(self, ui):
        ui.expander.return_value = nullcontext()
        ui.form.return_value = nullcontext()
        ui.selectbox.side_effect = ["US", "US_ALL", "XNAS:AAPL", "orb_vwap"]
        ui.number_input.return_value = 1
        ui.form_submit_button.return_value = True
        ui.button.return_value = False
        ui.session_state = {}
        market = {
            "resolved_market": "US", "label": "US - NYSE/NASDAQ", "exchange": "NYSE/NASDAQ",
            "currency": "USD", "timezone": "America/New_York", "session": "REGULAR", "status": "OPEN",
            "local_timestamp": "2026-10-08T12:00:00-04:00",
            "data_provider": "mock", "resolution_reason": "EXPLICIT_SELECTION",
            "ai_status": "CONFIGURATION_MISSING", "limitations": [],
            "profiles": [{"market": "US", "label": "US - NYSE/NASDAQ"}],
            "universes": [{"universe_id": "US_ALL", "name": "US universe",
                           "instrument_count": 1, "eligible_metadata_count": 1}],
            "default_universe_id": "US_ALL", "supported_strategies": ["orb_vwap"],
        }
        client = Mock()
        client.get.side_effect = [
            {"discovery": {}}, market, market,
            [{"instrument_id": "XNAS:AAPL", "market": "US"}],
        ]
        client.post.side_effect = RuntimeError("stop after checking submission")
        with self.assertRaises(RuntimeError):
            dashboard._personal_research(client)
        path, payload = client.post.call_args.args
        self.assertEqual(path, "/research/personal/runs")
        self.assertEqual((payload["selected_market"], payload["universe_id"]), ("US", "US_ALL"))
        self.assertNotIn("BASE_CURRENCY", payload)
        self.assertNotIn("RESEARCH_SESSIONS", payload)
        client.get.assert_any_call("/instruments", market="US")
        self.assertTrue(any("deterministic research still available" in str(call) for call in ui.warning.call_args_list))
