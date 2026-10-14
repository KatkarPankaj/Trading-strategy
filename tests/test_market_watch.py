import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import pandas as pd
from fastapi.testclient import TestClient

from stockmarket.api.app import ApiContext, create_app
from stockmarket.core.data.factory import create_market_data_provider, market_provider_env
from stockmarket.core.data import ResilientProvider, DataPolicy
from stockmarket.core.resilience import RetryPolicy
from stockmarket.core.data.provider import DataUnavailable, Quote
from stockmarket.core.data.twelvedata import TwelveDataProvider
from stockmarket.core.data.yahoo import YahooProvider, yahoo_symbol
from stockmarket.core.executors import TradingMode
from stockmarket.core.market_watch import MarketWatch
from stockmarket.core.markets import MarketDefinition, MarketRegistry, SessionPhase, default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.paper_loop import PaperLoopSettings, PaperTradingLoop
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.security import Secret
from stockmarket.core.trading_calendar import TradingCalendar
from stockmarket.dashboard import app as dashboard
from stockmarket.dashboard.client import ApiClient, ApiError


NOW = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)


class MarketWatchTests(TestCase):
    def setUp(self):
        self.db = SQLiteDatabase()
        self.addCleanup(self.db.close)
        migrate(self.db)
        self.store = Store(self.db)
        self.markets = default_markets()
        self.us = self.markets.get("US").instrument(
            "ABC", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=.01)
        self.india = self.markets.get("IN").instrument(
            "ABC", mic="XNSE", asset_class=AssetClass.EQUITY, tick_size=.05, lot_size=5)
        self.instruments = {i.instrument_id: i for i in (self.us, self.india)}
        for instrument in self.instruments.values():
            self.store.instruments.save(instrument)
        self.clock = NOW
        self.providers = {}
        for market in ("US", "IN"):
            provider = Mock()
            provider.name = f"fixture-{market}"
            provider.provider_name_for.side_effect = lambda i: f"fixture-{i.market}"
            provider.get_quote.side_effect = lambda i: Quote(
                i.instrument_id, 100, self.clock, f"fixture-{i.market}")
            self.providers[market] = provider
        self.watch = MarketWatch(
            self.instruments, self.markets, self.store.watchlist, self.providers,
            max_age={"US": timedelta(seconds=60), "IN": timedelta(seconds=30)},
            clock=lambda: self.clock)

    def test_search_uses_canonical_exchange_identity_and_pagination(self):
        result = self.watch.search("IN", "abc", limit=1)
        self.assertEqual(result["items"][0]["instrument_id"], "XNSE:ABC")
        self.assertEqual(result["items"][0]["currency"], "INR")
        self.assertEqual(result["items"][0]["lot_size"], 5)
        self.assertEqual(self.watch.search("US")["items"][0]["instrument_id"], "XNAS:ABC")
        self.assertEqual(self.watch.search("IN", after="XNSE:ABC")["items"], [])
        with self.assertRaises(ValueError):
            self.watch.search("ZZ")

    def test_watchlist_persists_idempotently_without_quotes_or_orders(self):
        self.watch.add(self.india.instrument_id)
        self.watch.add(self.india.instrument_id)
        restarted = Store(self.db)
        self.assertEqual(len(restarted.watchlist.list()), 1)
        row = self.watch.rows("IN")[0]
        self.assertEqual(row["quote_status"], "NOT_FETCHED")
        self.assertIsNone(row["price"])
        self.providers["IN"].get_quote.assert_not_called()
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
        self.watch.remove(self.india.instrument_id)
        self.assertEqual(self.watch.rows("IN"), [])

    def test_unknown_symbols_are_not_implicitly_created(self):
        for identity in ("ABC", "XNSE:UNKNOWN"):
            with self.assertRaises(ValueError):
                self.watch.add(identity)
        self.assertEqual(len(self.store.instruments.list()), 2)

    def test_disabling_a_market_preserves_members_without_breaking_other_market(self):
        self.watch.add(self.india.instrument_id)
        self.watch.add(self.us.instrument_id)
        us_only = MarketWatch(
            self.instruments, self.markets, self.store.watchlist, {"US": self.providers["US"]},
            max_age={"US": timedelta(seconds=60)}, clock=lambda: self.clock)
        self.assertEqual(len(us_only.rows("US")), 1)
        self.assertEqual(len(self.store.watchlist.list()), 2)

    def test_freshness_expires_per_market_without_provider_calls_on_get(self):
        for instrument in self.instruments.values():
            self.watch.add(instrument.instrument_id)
            self.watch.refresh(instrument.market)
        self.clock += timedelta(seconds=31)
        self.assertEqual(self.watch.rows("US")[0]["quote_status"], "FRESH")
        self.assertEqual(self.watch.rows("IN")[0]["quote_status"], "STALE")
        self.assertEqual(self.watch.rows("IN")[0]["price"], 100)
        self.assertFalse(self.watch.rows("US")[0]["eligible_for_order"])
        self.providers["US"].get_quote.assert_called_once()

    def test_failure_clears_old_price_and_does_not_change_other_market(self):
        for instrument in self.instruments.values():
            self.watch.add(instrument.instrument_id)
            self.watch.refresh(instrument.market)
        self.providers["IN"].get_quote.side_effect = DataUnavailable("fixture outage")
        self.watch.refresh("IN")
        self.assertEqual(self.watch.rows("IN")[0]["quote_status"], "DATA_UNAVAILABLE")
        self.assertIsNone(self.watch.rows("IN")[0]["price"])
        self.assertEqual(self.watch.rows("US")[0]["quote_status"], "FRESH")
        self.assertEqual(self.watch.profiles()[1]["market"], "US")

    def test_wrong_identity_source_future_off_tick_and_zero_are_invalid(self):
        self.watch.add(self.india.instrument_id)
        valid = Quote(self.india.instrument_id, 100, NOW, "fixture-IN")
        for quote in (replace(valid, instrument_id=self.us.instrument_id),
                      replace(valid, provider="wrong"),
                      replace(valid, price=0), replace(valid, price=100.01),
                      replace(valid, timestamp=NOW + timedelta(seconds=1))):
            with self.subTest(quote=quote):
                self.providers["IN"].get_quote.side_effect = None
                self.providers["IN"].get_quote.return_value = quote
                self.watch.refresh("IN")
                self.assertEqual(self.watch.rows("IN")[0]["quote_status"], "INVALID")
                self.assertIsNone(self.watch.rows("IN")[0]["price"])

    def test_slow_india_refresh_does_not_hold_us_or_watchlist_lock(self):
        entered, release = Event(), Event()
        for instrument in self.instruments.values():
            self.watch.add(instrument.instrument_id)

        def slow(instrument):
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture was not released")
            return Quote(instrument.instrument_id, 100, NOW, "fixture-IN")

        self.providers["IN"].get_quote.side_effect = slow
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.watch.refresh, "IN")
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.watch.refresh("US")["checked"], 1)
                self.assertEqual(self.watch.rows("US")[0]["quote_status"], "FRESH")
                with self.assertRaisesRegex(ValueError, "in progress"):
                    self.watch.refresh("IN")
            finally:
                release.set()
            self.assertEqual(first.result(5)["checked"], 1)

    def test_refresh_budget_is_explicitly_incomplete(self):
        self.watch.add(self.india.instrument_id)
        with patch("stockmarket.core.market_watch.time.monotonic", side_effect=[0, 6]):
            result = self.watch.refresh("IN")
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["items"][0]["quote_status"], "NOT_FETCHED")

    def test_authenticated_api_gets_are_read_only_and_writes_do_not_trade(self):
        context = ApiContext(
            settings=SimpleNamespace(), paper=SimpleNamespace(mode=TradingMode.PAPER),
            live=None, store=self.store, health=None, markets=self.markets,
            instruments=self.instruments, market_watch=self.watch,
            api_token=Secret("watch-test-token"))
        client = TestClient(create_app(context))
        headers = {"Authorization": "Bearer watch-test-token"}
        self.assertEqual(client.get("/watchlist", params={"market": "IN"}).status_code, 401)
        for url in ("/market-watch/markets", "/market-watch/instruments?market=IN",
                    "/watchlist?market=US"):
            self.assertEqual(client.get(url, headers=headers).status_code, 200)
        self.assertEqual(client.post("/watchlist", headers=headers,
                                    json={"instrument_id": self.india.instrument_id}).status_code, 200)
        response = client.post("/market-watch/refresh?market=IN", headers=headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["quote_status"], "FRESH")
        self.assertEqual(client.get("/watchlist?market=ZZ", headers=headers).status_code, 422)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
        self.assertEqual(client.post("/watchlist/remove", headers=headers,
                                    json={"instrument_id": self.india.instrument_id}).status_code, 200)

    def test_provider_alias_does_not_change_canonical_instrument(self):
        bse = self.markets.get("IN").instrument(
            "RELIANCE", mic="XBOM", asset_class=AssetClass.EQUITY, tick_size=.05)
        download = Mock(return_value=pd.DataFrame())
        yahoo = YahooProvider({bse.instrument_id: bse}, downloader=download,
                              symbol_map={bse.instrument_id: "500325.BO"})
        yahoo.get_ohlcv(bse, "1m", NOW - timedelta(minutes=1), NOW)
        self.assertEqual(download.call_args.kwargs["tickers"], "500325.BO")
        self.assertEqual(bse.instrument_id, "XBOM:RELIANCE")
        self.assertEqual(yahoo_symbol(self.india), "ABC.NS")
        self.assertEqual(yahoo_symbol(self.us), "ABC")

    def test_twelvedata_alias_retains_exchange_and_validates_returned_symbol(self):
        transport = Mock(return_value={"meta": {"symbol": "ALIAS", "mic_code": "XNSE"},
                                       "values": []})
        provider = TwelveDataProvider(
            self.instruments, "fixture-key", transport=transport,
            symbol_map={self.india.instrument_id: "ALIAS"})
        provider.get_ohlcv(self.india, "1m", NOW - timedelta(minutes=1), NOW)
        params = transport.call_args.args[0]
        self.assertEqual(params["symbol"], "ALIAS")
        self.assertEqual(params["mic_code"], "XNSE")

    def test_bad_provider_alias_configuration_rejected(self):
        with self.assertRaises(ValueError):
            create_market_data_provider("yahoo", self.instruments, markets=self.markets,
                                        env={"YAHOO_SYMBOL_MAP": '{"UNKNOWN":"ABC"}'})

    def test_market_credentials_do_not_mix_global_inline_and_scoped_file(self):
        env = {"TWELVEDATA_API_KEY": "global-fixture",
               "TWELVEDATA_API_KEY_FILE_IN": "fixture-key-path",
               "TWELVEDATA_REQUESTS_PER_MINUTE_IN": "4"}
        scoped = market_provider_env(env, "IN")
        self.assertNotIn("TWELVEDATA_API_KEY", scoped)
        self.assertEqual(scoped["TWELVEDATA_API_KEY_FILE"], "fixture-key-path")
        self.assertEqual(scoped["TWELVEDATA_REQUESTS_PER_MINUTE"], "4")
        self.assertEqual(market_provider_env(env, "US")["TWELVEDATA_API_KEY"], "global-fixture")

    def test_timed_out_late_quote_cannot_publish_and_workers_remain_bounded(self):
        entered, release = Event(), Event()

        def slow(instrument):
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture was not released")
            return Quote(instrument.instrument_id, 100, NOW, "fixture-IN")

        raw = self.providers["IN"]
        raw.get_quote.side_effect = slow
        bounded = ResilientProvider(
            raw, clock=lambda: NOW,
            policy=DataPolicy(timeout_seconds=.01, max_rate_limit_wait=0,
                              retry=RetryPolicy(max_attempts=1)))
        self.addCleanup(bounded.close)
        self.watch.providers["IN"] = bounded
        self.watch.add(self.india.instrument_id)
        try:
            for _ in range(5):
                self.watch.refresh("IN")
            self.assertTrue(entered.is_set())
            self.assertLessEqual(raw.get_quote.call_count, 4)
            self.assertIsNone(self.watch.rows("IN")[0]["price"])
            self.watch.add(self.us.instrument_id)
            self.assertEqual(self.watch.refresh("US")["items"][0]["quote_status"], "FRESH")
        finally:
            release.set()
        self.assertIsNone(self.watch.rows("IN")[0]["price"])

    @patch.object(dashboard, "st")
    def test_dashboard_get_does_not_fetch_quotes_or_submit_orders(self, ui):
        ui.selectbox.return_value = "IN"
        ui.text_input.return_value = "abc"
        ui.button.return_value = False
        client = Mock()
        client.get.side_effect = [
            self.watch.profiles(),
            {"execution_market": "US", "india_status": "BLOCKED", "india": []},
            {"items": [], "next_cursor": None}, []]
        dashboard._market_watch(client)
        self.assertEqual(client.get.call_args_list[-1].kwargs, {"market": "IN"})
        client.post.assert_not_called()

    def test_dashboard_displays_watchlist_validation_reason(self):
        response = Mock(status_code=422, ok=False)
        response.json.return_value = {"detail": "quote refresh already in progress for this market"}
        with self.assertRaisesRegex(ApiError, "already in progress"):
            ApiClient._response_json(response, "/market-watch/refresh?market=IN")


class MultiMarketCalendarTests(TestCase):
    def registry(self):
        registry = MarketRegistry()
        for code, zone, hours in (("US", "America/New_York", (time(9, 30), time(16))),
                                  ("IN", "Asia/Kolkata", (time(9, 15), time(15, 30)))):
            registry.register(MarketDefinition(
                code, code, ("XNAS",) if code == "US" else ("XNSE", "XBOM"),
                "USD" if code == "US" else "INR",
                TradingCalendar(zone, *hours, covered_years=frozenset({2026}))))
        return registry

    def test_india_session_does_not_depend_on_us_closed_session(self):
        registry = self.registry()
        self.assertTrue(registry.is_regular_session("IN", NOW))
        self.assertFalse(registry.is_regular_session("US", NOW))
        us_open = NOW.replace(hour=15)
        self.assertTrue(registry.is_regular_session("US", us_open))
        self.assertFalse(registry.is_regular_session("IN", us_open))

    def test_us_dst_and_india_fixed_offset(self):
        registry = self.registry()
        self.assertTrue(registry.is_regular_session(
            "US", datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)))
        self.assertFalse(registry.is_regular_session(
            "US", datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc)))
        self.assertTrue(registry.is_regular_session(
            "US", datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)))
        for day in (datetime(2026, 10, 30, 3, 45, tzinfo=timezone.utc),
                    datetime(2026, 11, 2, 3, 45, tzinfo=timezone.utc)):
            self.assertTrue(registry.is_regular_session("IN", day))

    def test_exchange_overrides_and_uncovered_dates_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for mic, holidays in (("XNSE", ["2026-10-14"]), ("XBOM", [])):
                (path / f"{mic}.json").write_text(json.dumps({
                    "years": [2026], "holidays": holidays, "early_closes": {}}), encoding="utf-8")
            india = default_markets(path).get("IN")
            self.assertEqual(india.phase(NOW, "XNSE"), SessionPhase.CLOSED)
            self.assertEqual(india.phase(NOW, "XBOM"), SessionPhase.CLOSED)
            self.assertEqual(india.phase(NOW), SessionPhase.CLOSED)
            self.assertFalse(india.is_covered(date(2027, 1, 1)))
        self.assertEqual(default_markets().get("IN").phase(NOW), SessionPhase.CLOSED)

    def test_market_specific_scheduling_and_flatten(self):
        registry = self.registry()
        clock = {"now": NOW}
        loops = {}
        for code in ("IN", "US"):
            positions = SimpleNamespace(trading=SimpleNamespace(mode=TradingMode.PAPER),
                                        manage=Mock(return_value={"positions": []}),
                                        flatten=Mock(return_value={"positions": []}))
            cycles = Mock()
            cycles.run.return_value = {"run_id": code, "status": "COMPLETE"}
            loops[code] = PaperTradingLoop(
                cycles=cycles, positions=positions, gate=SimpleNamespace(halted=False),
                markets=registry, settings=PaperLoopSettings(universe_id=f"{code}_ALL", market=code),
                clock=lambda: clock["now"])
        self.assertEqual(loops["IN"].tick()["action"], "CYCLE")
        self.assertEqual(loops["US"].tick()["action"], "MARKET_CLOSED")
        clock["now"] = NOW.replace(hour=9, minute=52)
        self.assertEqual(loops["IN"].tick()["action"], "END_OF_DAY_FLATTEN")
        loops["IN"].positions.flatten.assert_called_once_with(market="IN")
        self.assertEqual(loops["US"].tick()["action"], "MARKET_CLOSED")
