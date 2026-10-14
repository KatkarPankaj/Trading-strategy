"""Synthetic safety fixtures, not evidence of verified exchange coverage or feed approval."""

import hashlib
import json
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from stockmarket.api.app import create_app
from stockmarket.api.bootstrap import build_context
from stockmarket.core.india_readiness import IndiaPaperReadiness
from stockmarket.core.markets import default_markets, load_calendar_file, SessionPhase
from stockmarket.core.models import AssetClass
from stockmarket.core.paper_worker import PaperWorkerLease
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.settings import ConfigurationError, load_settings
from stockmarket.core.trading_calendar import TradingCalendar

from test_paper_end_to_end import ENV, Clock, ControlledData, market_stats


NOW = datetime(2026, 10, 14, 5, tzinfo=timezone.utc)


def calendar_fixture(directory, mic="XNSE"):
    """Local publication bytes intentionally contain no real exchange schedule."""
    path = Path(directory)
    artifact = path / f"{mic}-fixture.txt"
    artifact.write_text("SYNTHETIC PUBLICATION: never use for execution", encoding="utf-8")
    source_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    schedule = {
        "years": [2026], "holidays": ["2026-01-26", "2026-11-07"],
        "early_closes": {"2026-10-15": "13:00"},
        "special_sessions": {"2026-11-07": [["18:00", "19:00"], ["19:30", "20:00"]]},
    }
    digest = hashlib.sha256(json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dates = ["2026-01-26", "2026-10-15", "2026-11-07"]
    report = {"status": "REVIEWED", "reviewer": "SYNTHETIC TEST FIXTURE",
              "schedule_sha256": digest, "verified_dates": dates,
              "date_evidence": {d: {"source_sha256": source_hash, "locator": "fixture line 1"} for d in dates}}
    report_name = f"{mic}-report.json"
    (path / report_name).write_text(json.dumps(report), encoding="utf-8")
    host = "nseindia.com" if mic == "XNSE" else "bseindia.com"
    data = dict(schedule, review={
        "exchange": mic, "segment": "CASH_EQUITY",
        "retrieved_at": "2026-01-01T00:00:00+00:00", "reviewed_at": "2026-01-02T00:00:00+00:00",
        "coverage_start": "2026-01-01", "coverage_end": "2026-12-31", "expires_on": "2026-12-31",
        "sources": [{"url": f"https://www.{host}/fixture-not-a-real-publication",
                     "artifact": artifact.name, "sha256": source_hash}],
        "validation_report": report_name,
    })
    target = path / f"{mic}.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target, data


class IndiaCalendarSafetyTests(TestCase):
    def test_reviewed_fixture_regular_holiday_weekend_and_special_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            target, _ = calendar_fixture(directory)
            cal = TradingCalendar("Asia/Kolkata", time(9, 15), time(15, 30),
                                  **load_calendar_file(target, reviewed_exchange="XNSE"))
            self.assertTrue(cal.is_open(NOW))
            self.assertFalse(cal.is_trading_day(date(2026, 1, 26)))
            self.assertFalse(cal.is_trading_day(date(2026, 11, 8)))
            self.assertEqual(cal.close_time_on(date(2026, 10, 15)), time(13))
            self.assertEqual(cal.close_time_on(date(2026, 11, 7)), time(20))
            for local_hour, minute, expected in [(17, 59, False), (18, 0, True),
                                                  (19, 0, False), (19, 30, True), (20, 0, False)]:
                at = datetime(2026, 11, 7, local_hour, minute,
                              tzinfo=timezone(timedelta(hours=5, minutes=30)))
                self.assertEqual(cal.is_open(at), expected)
            self.assertFalse(cal.is_covered(date(2027, 1, 1)))

    def test_missing_unreviewed_empty_country_fallback_never_authorizes_india(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ("IN", "XNSE", "XBOM"):
                (path / f"{name}.json").write_text(
                    json.dumps({"years": [2026], "holidays": []}), encoding="utf-8")
            markets = default_markets(path)
            self.assertFalse(markets.get("IN").is_covered(NOW.date()))
            self.assertEqual(markets.get("IN").phase(NOW, "XNSE"), SessionPhase.CLOSED)
        self.assertFalse(default_markets().get("IN").is_covered(NOW.date()))

    def test_each_venue_needs_its_own_review_and_special_session_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            calendar_fixture(directory)
            india = default_markets(Path(directory)).get("IN")
            self.assertEqual(india.phase(NOW, "XNSE"), SessionPhase.REGULAR)
            self.assertEqual(india.phase(NOW, "XBOM"), SessionPhase.CLOSED)
            self.assertFalse(india.is_covered(NOW.date()))
            for hour, expected in [(12, SessionPhase.CLOSED), (13, SessionPhase.REGULAR),
                                   (15, SessionPhase.CLOSED)]:
                self.assertEqual(india.phase(datetime(2026, 11, 7, hour, tzinfo=timezone.utc),
                                             "XNSE"), expected)

    def test_tampering_or_missing_date_evidence_is_rejected(self):
        mutations = [
            lambda d: d["holidays"].append("2026-10-14"),
            lambda d: d["review"].update(exchange="XBOM"),
            lambda d: d["review"].update(segment="DERIVATIVES"),
            lambda d: d["review"].update(reviewed_at="2026-01-02T00:00:00"),
            lambda d: d["review"].update(coverage_end="2025-12-31"),
            lambda d: d["review"].update(validation_report="..\\outside.json"),
            lambda d: d["review"]["sources"][0].update(url="https://nseindia.com.example.org/x"),
            lambda d: d["review"]["sources"][0].update(sha256="0" * 64),
            lambda d: d.update(holidays=[]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                target, data = calendar_fixture(directory)
                mutate(data)
                target.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_calendar_file(target, reviewed_exchange="XNSE")
        with tempfile.TemporaryDirectory() as directory:
            target, data = calendar_fixture(directory)
            report = Path(directory) / data["review"]["validation_report"]
            payload = json.loads(report.read_text())
            del payload["date_evidence"]["2026-01-26"]
            report.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "each calendar date"):
                load_calendar_file(target, reviewed_exchange="XNSE")

    def test_expired_review_and_coverage_boundary_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            target, data = calendar_fixture(directory)
            data["review"]["coverage_start"] = "2026-10-01"
            # Existing January evidence is outside coverage, never silently discarded.
            target.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside reviewed coverage"):
                load_calendar_file(target, reviewed_exchange="XNSE")
            target, data = calendar_fixture(directory)
            data["review"]["expires_on"] = "2026-10-13"
            target.write_text(json.dumps(data), encoding="utf-8")
            cal = TradingCalendar("Asia/Kolkata", time(9, 15), time(15, 30),
                                  **load_calendar_file(target, reviewed_exchange="XNSE"))
            self.assertFalse(cal.is_open(NOW))

    def test_malformed_special_intervals_and_json_are_rejected(self):
        for sessions in ({date(2026, 11, 7): ()},
                         {date(2026, 11, 7): ((time(19), time(18)),)},
                         {date(2026, 11, 7): ((time(18), time(20)), (time(19), time(21)))}):
            with self.assertRaises(ValueError):
                TradingCalendar("Asia/Kolkata", time(9, 15), time(15, 30), special_sessions=sessions)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "XNSE.json"
            target.write_text("{broken")
            with self.assertRaises(ValueError):
                load_calendar_file(target, reviewed_exchange="XNSE")

    def test_special_intervals_copy_inputs_and_conflicting_close_is_rejected(self):
        sessions = {date(2026, 11, 7): ((time(18), time(19)),)}
        cal = TradingCalendar("Asia/Kolkata", time(9, 15), time(15, 30), special_sessions=sessions)
        sessions.clear()
        self.assertEqual(cal.close_time_on(date(2026, 11, 7)), time(19))
        from dataclasses import asdict
        self.assertEqual(asdict(cal)["special_sessions"][date(2026, 11, 7)],
                         ((time(18), time(19)),))
        with self.assertRaisesRegex(ValueError, "cannot also"):
            TradingCalendar("Asia/Kolkata", time(9, 15), time(15, 30),
                            early_closes={date(2026, 11, 7): time(13)},
                            special_sessions={date(2026, 11, 7): ((time(18), time(19)),)})

    def policy(self, markets, instrument):
        from stockmarket.core.strategy_lab import StrategyLabService
        lab = StrategyLabService.__new__(StrategyLabService)
        lab.instruments = {instrument.instrument_id: instrument}
        lab.assessment = SimpleNamespace(markets=markets)
        lab.sessions = {}
        lab.sizing = {}
        lab.flatten_minutes = 10
        return lab._evaluation_policy(
            {"proposal_id": "fixture", "parameters_hash": "fixture",
             "instrument_id": instrument.instrument_id}, SimpleNamespace(timeframe="5m"),
            NOW - timedelta(days=1), NOW, .001, 1)

    def test_unchanged_us_calendar_policy_retains_legacy_fingerprint_shape(self):
        from dataclasses import asdict
        from stockmarket.core.persistence.repositories import to_json
        markets = default_markets()
        market = markets.get("US")
        instrument = market.instrument("AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=.01)
        legacy = asdict(market.calendar)
        for key in ("special_sessions", "coverage_start", "coverage_end",
                    "review_required", "review_expires", "provenance"):
            del legacy[key]
        legacy["early_closes"] = {d.isoformat(): t.isoformat() for d, t in market.calendar.early_closes.items()}
        self.assertEqual(self.policy(markets, instrument)["calendar"], json.loads(to_json(legacy)))

    def test_reviewed_venue_policy_preserves_special_timings_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            calendar_fixture(directory)
            markets = default_markets(Path(directory))
            instrument = markets.get("IN").instrument(
                "TEST", mic="XNSE", asset_class=AssetClass.EQUITY, tick_size=.05)
            policy = self.policy(markets, instrument)["calendar"]
            self.assertEqual(policy["provenance"]["exchange"], "XNSE")
            self.assertEqual(policy["special_sessions"]["2026-11-07"],
                             [["18:00:00", "19:00:00"], ["19:30:00", "20:00:00"]])


class IndiaReadinessTests(TestCase):
    def setUp(self):
        self.markets = default_markets()
        self.instrument = self.markets.get("IN").instrument(
            "TEST", mic="XNSE", asset_class=AssetClass.EQUITY, tick_size=.05)
        self.recovery = SimpleNamespace(execution_ready=True)
        self.provider = SimpleNamespace(name="mock")

    def readiness(self, base="INR", cash=100000, explicit=True, market="IN"):
        return IndiaPaperReadiness(
            execution_market=market, instruments={self.instrument.instrument_id: self.instrument},
            markets=self.markets, portfolio=PortfolioManager(base, cash),
            provider=self.provider, recovery=self.recovery,
            starting_cash_explicit=explicit, clock=lambda: NOW)

    def test_granular_readiness_not_green_for_observation_or_explicit_cash(self):
        row = self.readiness().instrument_status(self.instrument.instrument_id)
        self.assertTrue(row["checks"]["observation_enabled"])
        self.assertTrue(row["checks"]["funding_currency_validated"])
        self.assertTrue(row["checks"]["paper_recovery_complete"])
        self.assertFalse(row["checks"]["calendar_verified"])
        self.assertFalse(row["checks"]["instrument_mapping_verified"])
        self.assertFalse(row["checks"]["provider_validated"])
        self.assertFalse(row["execution_eligible"])
        self.assertFalse(row["risk_approval"])

    def test_usd_default_zero_cash_or_missing_explicit_cash_never_becomes_inr(self):
        for options in ({"base": "USD"}, {"cash": 0}, {"explicit": False}):
            with self.subTest(options=options):
                readiness = self.readiness(**options)
                row = readiness.instrument_status(self.instrument.instrument_id)
                self.assertFalse(row["checks"]["funding_currency_validated"])
                self.assertIn("EXPLICIT_INR_BASE_CASH_REQUIRED_NO_IMPLICIT_FX", row["blockers"])

    def test_recovery_failure_and_market_mismatch_are_rechecked(self):
        readiness = self.readiness(market="US")
        self.assertEqual(readiness.blocker(self.instrument.instrument_id), "EXECUTION_MARKET_MISMATCH")
        readiness = self.readiness()
        self.recovery.execution_ready = False
        self.assertIn("PAPER_RECOVERY_INCOMPLETE", readiness.blocker(self.instrument.instrument_id))
        self.recovery.execution_ready = True
        self.assertNotIn("PAPER_RECOVERY_INCOMPLETE", readiness.blocker(self.instrument.instrument_id))

    def test_instrument_identity_currency_and_tradability_are_checked(self):
        for changes in ({"currency": "USD"}, {"tradable": False}, {"active": False},
                        {"instrument_id": "XNSE:WRONG"}, {"timezone": "America/New_York"}):
            self.instrument = replace(self.instrument, **changes)
            row = self.readiness().instrument_status(self.instrument.instrument_id)
            self.assertFalse(row["checks"]["instrument_metadata_valid"])


class IndiaBootstrapTests(TestCase):
    def setUp(self):
        self.db = SQLiteDatabase()
        migrate(self.db)
        self.store = Store(self.db)
        self.addCleanup(self.db.close)
        self.clock = Clock(NOW)
        self.data = ControlledData(self.clock)
        self.india = default_markets().get("IN").instrument(
            "TEST", mic="XNSE", asset_class=AssetClass.EQUITY, tick_size=.05)
        self.store.instruments.save(self.india)

    def context(self, env=None, store=None):
        env = env or dict(ENV, MARKETS="US,IN")
        ctx = build_context(load_settings(env), env, store=store or self.store,
                            market_stats=market_stats, clock=self.clock, market_data_provider=self.data)
        self.addCleanup(ctx.market_data.close)
        self.addCleanup(ctx.market_watch.close)
        if ctx.worker_lease:
            self.addCleanup(ctx.worker_lease.close)
        return ctx

    def test_default_us_worker_unchanged_and_watch_selection_does_not_switch_it(self):
        ctx = self.context()
        self.assertEqual(ctx.india_readiness.execution_market, "US")
        self.assertIsNone(ctx.paper_loop)
        client = TestClient(create_app(ctx))
        self.addCleanup(client.close)
        headers = {"Authorization": f"Bearer {ENV['API_TOKEN']}"}
        response = client.get("/paper/market-readiness", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution_market"], "US")
        client.get("/market-watch/instruments?market=IN", headers=headers)
        self.assertEqual(ctx.india_readiness.execution_market, "US")
        health = client.get("/health").json()
        self.assertEqual(health["paper_readiness"]["execution_market"], "US")
        self.assertIn("NO_REGULAR_MARKET_SESSION", health["paper_readiness"]["blockers"])
        self.assertEqual(health["dashboard_contract"]["version"], 1)
        self.assertIn("execution_data_status", health["dashboard_contract"]["features"])

    def test_health_does_not_mask_stale_quotes_when_us_is_closed_or_open(self):
        from stockmarket.core.api_contract import dashboard_compatibility_problem
        ctx = self.context()
        client = TestClient(create_app(ctx))
        self.addCleanup(client.close)
        ctx.health.record_market_data(NOW - timedelta(hours=10))
        for hour, expected in ((5, "MISSING_OR_STALE_OUTSIDE_SESSION"),
                               (15, "MISSING_OR_STALE_DURING_SESSION")):
            self.clock.at = NOW.replace(hour=hour)
            report = client.get("/health").json()
            self.assertIsNone(dashboard_compatibility_problem(report))
            data = report["execution_data_status"]
            self.assertEqual(data["market"], "US")
            self.assertEqual(data["status"], expected)
            self.assertFalse(data["entry_data_ready"])
            self.assertEqual(data["source_timestamp"], (NOW - timedelta(hours=10)).isoformat())
            self.assertIn("MISSING_OR_STALE_EXECUTION_DATA", report["paper_readiness"]["blockers"])

    def test_india_worker_requires_explicit_inr_and_no_fx(self):
        for changes in ({}, {"BASE_CURRENCY": "INR", "PAPER_STARTING_CASH": ""},
                        {"BASE_CURRENCY": "INR", "FX_RATES": "USD=84"}):
            env = dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="IN")
            env.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                self.context(env)
        self.assertIsNone(self.store.audit.latest(("PAPER_WORKER_BOUND",)))

    def test_explicit_india_cash_persists_but_feed_calendar_still_block(self):
        env = dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="IN", BASE_CURRENCY="INR")
        ctx = self.context(env)
        self.assertEqual(ctx.paper.portfolio.cash, {"INR": 100000})
        self.assertTrue(ctx.recovery.execution_ready)
        status = ctx.india_readiness.instrument_status(self.india.instrument_id)
        self.assertFalse(status["execution_eligible"])
        self.assertIn("INDIA_EXECUTION_PROVIDER_NOT_VALIDATED", status["blockers"])
        self.assertEqual(json.loads(self.store.audit.latest(("PAPER_WORKER_BOUND",))["payload"])["market"], "IN")
        restarted = self.context(env)
        self.assertEqual(restarted.paper.portfolio.cash, ctx.paper.portfolio.cash)
        self.assertFalse(restarted.india_readiness.status()["india"][0]["execution_eligible"])

    def test_restart_refuses_market_currency_or_cash_reinterpretation(self):
        self.context()
        for changes in ({"PAPER_EXECUTION_MARKET": "IN", "BASE_CURRENCY": "INR"},
                        {"BASE_CURRENCY": "EUR"}, {"PAPER_STARTING_CASH": "500000"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ConfigurationError, "persisted paper worker"):
                self.context(dict(ENV, MARKETS="US,IN", **changes))

    def test_conflicting_market_configuration_is_startup_error(self):
        with self.assertRaisesRegex(ConfigurationError, "conflicts"):
            self.context(dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="US",
                              PAPER_AUTO_LOOP_MARKET="IN"))

    def test_unverified_india_entry_is_rejected_before_provider_or_broker(self):
        from stockmarket.core.trading_service import OrderTicket
        from stockmarket.core.models import OrderSide, OrderType, RiskDecisionStatus, OrderStatus
        env = dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="IN", BASE_CURRENCY="INR")
        ctx = self.context(env)
        ctx.paper.quotes = Mock(side_effect=AssertionError("must not obtain quote for unready entry"))
        ticket = OrderTicket(self.india.instrument_id, OrderSide.BUY, 1, OrderType.MARKET, "fixture")
        result = ctx.paper.submit(ticket)
        self.assertEqual(result.risk.status, RiskDecisionStatus.REJECTED)
        self.assertIn("INDIA_CALENDAR", result.risk.reason)
        self.assertEqual(result.order.status, OrderStatus.REJECTED)
        ctx.paper.quotes.assert_not_called()
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM fills")[0]["n"], 0)

    def test_final_entry_gate_rechecks_prerequisites_after_preparation(self):
        from stockmarket.core.trading_service import OrderTicket
        from stockmarket.core.models import OrderSide, OrderType, RiskDecisionStatus
        env = dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="IN", BASE_CURRENCY="INR")
        ctx = self.context(env)
        ticket = OrderTicket(self.india.instrument_id, OrderSide.BUY, 1, OrderType.MARKET, "fixture")
        # Fixture-only first admission; production adapters/calendar remain blocked.
        with patch.object(ctx.india_readiness, "blocker", side_effect=[
                None, "EXPLICIT_INR_BASE_CASH_REQUIRED_NO_IMPLICIT_FX"]) as admission:
            result = ctx.paper.submit(ticket)
        self.assertEqual(admission.call_count, 2)
        self.assertEqual(result.risk.status, RiskDecisionStatus.REJECTED)
        self.assertIn("EXPLICIT_INR", result.risk.reason)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM fills")[0]["n"], 0)

    def test_interrupted_replay_cannot_be_resumed_or_reported_complete(self):
        from stockmarket.core.recovery import RecoveryError
        env = dict(ENV, MARKETS="US,IN", PAPER_EXECUTION_MARKET="IN", BASE_CURRENCY="INR")
        ctx = self.context(env)
        with patch.object(ctx.recovery, "_restore_orders", side_effect=RuntimeError("interrupted page")):
            with self.assertRaises(RecoveryError):
                ctx.recovery.recover()
        self.assertFalse(ctx.recovery.execution_ready)
        self.assertFalse(ctx.india_readiness.instrument_status(self.india.instrument_id)[
            "checks"]["paper_recovery_complete"])
        with self.assertRaisesRegex(RecoveryError, "replay incomplete"):
            ctx.recovery.resume("fixture", "must not bypass incomplete replay")
        self.assertTrue(ctx.recovery.recover().clean)
        self.assertTrue(ctx.recovery.execution_ready)

    def test_mixed_market_loop_universe_rejected_before_worker_start(self):
        from stockmarket.core.ai import AIAnalyst
        from test_paper_end_to_end import AdvisoryAI
        from stockmarket.core.strategies import OrbVwapStrategy
        env = dict(ENV, MARKETS="US,IN", PAPER_AUTO_LOOP="true",
                   PAPER_EXECUTION_MARKET="US", PAPER_AUTO_LOOP_UNIVERSE="IN_ALL")
        with self.assertRaisesRegex(ConfigurationError, "only the execution market"):
            build_context(load_settings(env), env, store=self.store, market_stats=market_stats,
                          clock=self.clock, market_data_provider=self.data,
                          research_analyst=AIAnalyst(AdvisoryAI(), clock=self.clock),
                          research_strategies={"orb_vwap": OrbVwapStrategy()})

    def test_shutdown_retains_ownership_when_monitor_does_not_stop(self):
        ctx = self.context()
        ctx.monitor = Mock()
        ctx.monitor.stop.side_effect = RuntimeError("monitor still processing")
        ctx.worker_lease = Mock()
        application = create_app(ctx)
        with patch.object(ctx.market_data, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "still processing"):
                application.router.on_shutdown[0]()
            close.assert_not_called()
        ctx.worker_lease.close.assert_not_called()

    def test_automatic_kill_monitor_starts_on_asgi_startup_not_composition(self):
        env = dict(ENV, MARKETS="US,IN", KILL_SWITCH_AUTO="true")
        with patch("stockmarket.api.bootstrap.AutoTriggerMonitor.start") as start:
            ctx = self.context(env)
            start.assert_not_called()
            application = create_app(ctx)
            application.router.on_startup[0]()
            start.assert_called_once_with(5.0)


class WorkerOwnershipTests(TestCase):
    def test_file_worker_exclusion_release_and_distinct_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            lease = PaperWorkerLease(path)
            try:
                with self.assertRaisesRegex(ValueError, "ALREADY_OWNED"):
                    PaperWorkerLease(path)
                separate = PaperWorkerLease(str(Path(directory) / "india.db"))
                separate.close()
            finally:
                lease.close()
            restarted = PaperWorkerLease(path)
            restarted.close()
            restarted.close()

    def test_bootstrap_failure_releases_file_worker_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteDatabase(str(Path(directory) / "paper.db"))
            migrate(db)
            store = Store(db)
            try:
                # No verified master: startup fails after ownership acquisition.
                env = dict(ENV, INSTRUMENT_BOOTSTRAP="unsupported")
                with self.assertRaises((ConfigurationError, ValueError)):
                    build_context(load_settings(env), env, store=store)
                lease = PaperWorkerLease(db.path)
                lease.close()
            finally:
                db.close()

    def test_process_interruption_releases_os_owned_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            code = (
                "import sys; from stockmarket.core.paper_worker import PaperWorkerLease; "
                "lease=PaperWorkerLease(sys.argv[1]); print('OWNED',flush=True); sys.stdin.readline()"
            )
            process = subprocess.Popen([sys.executable, "-c", code, path],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "OWNED")
                with self.assertRaisesRegex(ValueError, "ALREADY_OWNED"):
                    PaperWorkerLease(path)
                process.terminate()
                process.wait(timeout=10)
                restarted = PaperWorkerLease(path)
                restarted.close()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                process.communicate(timeout=10)

    def test_kill_monitor_failed_join_does_not_claim_shutdown_complete(self):
        from stockmarket.core.kill_switch import AutoTriggerMonitor
        monitor = AutoTriggerMonitor.__new__(AutoTriggerMonitor)
        from threading import Event
        monitor._stop = Event()
        monitor._thread = Mock()
        monitor._thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "ownership must remain held"):
            monitor.stop()
        self.assertTrue(monitor._stop.is_set())
        monitor._thread.join.assert_called_once_with(timeout=2.0)
