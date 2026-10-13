"""Kill-switch, authentication and live-readiness safety regressions (Milestone 2, Priority 2).

Every test runs the production composition (`build_context` + HTTP API) or the real settings and
readiness code. Nothing touches the network or a real broker.
"""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace
import unittest

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from stockmarket.api.app import create_app
from stockmarket.core.executors import TradingMode
from stockmarket.core.live_readiness import LiveReadinessChecker
from stockmarket.core.settings import ConfigurationError, load_settings

import test_paper_end_to_end as e2e_fixture
from test_paper_end_to_end import AS_OF, HEADERS, STARTING_CASH

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class Harness(unittest.TestCase):
    """Borrows the end-to-end fixture (store, clock, controlled data, advisory AI) without its tests."""

    def setUp(self):
        self.e2e = e2e_fixture.PaperEndToEndTests("test_risk_rejection_creates_no_order")
        self.e2e.setUp()
        self.addCleanup(self.e2e.doCleanups)
        self.store, self.clock, self.data = self.e2e.store, self.e2e.clock, self.e2e.data
        self.instrument_id = self.e2e.instrument_id

    def context(self):
        return self.e2e.context()

    def client(self, ctx):
        return self.e2e.client(ctx)

    @staticmethod
    def mutating_paths(app):
        for route in app.routes:
            if isinstance(route, APIRoute) and route.methods & MUTATING:
                for method in sorted(route.methods & MUTATING):
                    yield method, re.sub(r"\{[^}]+\}", "test", route.path)

    def entry_order(self, client, key):
        return client.post("/paper/orders", headers=HEADERS, json={
            "client_order_id": key, "instrument_id": self.instrument_id, "side": "BUY",
            "quantity": 1, "order_type": "MARKET", "strategy": "manual",
            "trade_intent": "BUY"})


class AuthenticationTests(Harness):
    def test_every_mutating_route_rejects_missing_or_wrong_token(self):
        ctx = self.context()
        client = self.client(ctx)
        paths = list(self.mutating_paths(client.app))
        self.assertGreaterEqual(len(paths), 20)
        for method, path in paths:
            with self.subTest(path=path):
                self.assertEqual(client.request(method, path, json={}).status_code, 401)
                wrong = {"Authorization": "Bearer not-the-token"}
                self.assertEqual(
                    client.request(method, path, json={}, headers=wrong).status_code, 401)
                basic = {"Authorization": HEADERS["Authorization"].replace("Bearer", "Basic")}
                self.assertEqual(
                    client.request(method, path, json={}, headers=basic).status_code, 401)
        self.assertEqual(self.store.orders.all(), [])

    def test_missing_api_token_disables_every_mutating_route(self):
        ctx = self.context()
        ctx.api_token = None
        client = self.client(ctx)
        for method, path in self.mutating_paths(client.app):
            with self.subTest(path=path):
                self.assertEqual(
                    client.request(method, path, json={}, headers=HEADERS).status_code, 503)
        self.assertEqual(self.store.orders.all(), [])


class KillSwitchTests(Harness):
    def test_kill_switch_blocks_entries_allows_exits_and_requires_audited_reset(self):
        client = self.client(self.context())
        proposal = self.e2e.propose(client)
        self.data.price = proposal["entry_price"]
        self.assertEqual(self.e2e.submit(client, proposal["proposal_id"]).status_code, 200)
        opened = self.e2e.state(client)
        self.assertEqual(len(opened["positions"]), 1)

        trigger = client.post("/kill-switch/trigger", headers=HEADERS, json={
            "operator": "safety-test", "reason": "regression"})
        self.assertEqual(trigger.status_code, 200, trigger.text)

        # A new manual entry and a fresh research proposal are both refused without mutation.
        blocked = self.entry_order(client, "blocked-entry")
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["order"]["status"], "REJECTED")
        self.assertIn("TRADING_HALTED", blocked.json()["risk_decision"]["reason"])
        after_block = self.e2e.state(client)
        self.assertEqual(after_block["fills"], opened["fills"])
        self.assertEqual(after_block["positions"], opened["positions"])
        self.assertEqual(after_block["cash"], opened["cash"])
        self.assertEqual(self.store.orders.get("blocked-entry")["filled_quantity"], 0)

        # Restart while halted: the switch re-engages from the audit log.
        restarted_ctx = self.context()
        self.assertTrue(restarted_ctx.kill_switch.active)
        restarted = self.client(restarted_ctx)
        self.assertEqual(self.entry_order(restarted, "blocked-after-restart")
                         .json()["order"]["status"], "REJECTED")

        # Documented policy: risk-reducing exits stay possible while entries are halted.
        self.clock.at = AS_OF + timedelta(minutes=10)
        self.data.price = round(proposal["provenance"]["signal_summary"]["target_price"] + 0.3, 2)
        managed = restarted.post("/paper/positions/manage", headers=HEADERS)
        self.assertEqual(managed.status_code, 200, managed.text)
        exit_order = managed.json()["positions"][0]["order"]
        self.assertEqual((exit_order["status"], exit_order["trade_intent"]),
                         ("FILLED", "SELL_TO_CLOSE"))
        self.assertEqual(self.e2e.state(restarted)["positions"], [])

        # Reset needs a token, operator and note; resetting an inactive switch is refused.
        self.assertEqual(restarted.post("/kill-switch/reset", json={
            "operator": "op", "note": "cleared"}).status_code, 401)
        self.assertEqual(restarted.post("/kill-switch/reset", headers=HEADERS, json={
            "operator": "op"}).status_code, 422)
        self.assertEqual(restarted.post("/kill-switch/reset", headers=HEADERS, json={
            "operator": " ", "note": "cleared"}).status_code, 409)
        self.assertTrue(restarted_ctx.kill_switch.active)
        reset = restarted.post("/kill-switch/reset", headers=HEADERS, json={
            "operator": "op", "note": "cleared"})
        self.assertEqual(reset.json(), {"active": False})
        self.assertEqual(restarted.post("/kill-switch/reset", headers=HEADERS, json={
            "operator": "op", "note": "again"}).status_code, 409)
        self.assertEqual(self.store.audit.latest(("KILL_SWITCH_RESET",))["actor"], "op")

    def test_research_proposal_submission_is_halted_without_mutation(self):
        client = self.client(self.context())
        proposal = self.e2e.propose(client)
        self.data.price = proposal["entry_price"]
        self.assertEqual(client.post("/kill-switch/trigger", headers=HEADERS, json={
            "operator": "safety-test", "reason": "regression"}).status_code, 200)
        response = self.e2e.submit(client, proposal["proposal_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["execution"], "PAPER_ORDER_REJECTED")
        self.assertIn("TRADING_HALTED", response.json()["order"]["error"])
        self.e2e.assert_untouched(client)

    def test_exit_without_a_position_is_rejected_while_halted(self):
        ctx = self.context()
        client = self.client(ctx)
        self.data.price = 102.0
        ctx.kill_switch.trigger("MANUAL", "test", source="manual", actor="safety-test")
        for side, intent in (("SELL", "SELL_TO_CLOSE"), ("BUY", "COVER")):
            with self.subTest(intent=intent):
                response = client.post("/paper/orders", headers=HEADERS, json={
                    "client_order_id": f"exit-{intent}", "instrument_id": self.instrument_id,
                    "side": side, "quantity": 1, "order_type": "MARKET", "strategy": "manual",
                    "intent": "EXIT", "trade_intent": intent})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["order"]["status"], "REJECTED")
                self.assertIn("EXIT_QUANTITY_EXCEEDS_POSITION",
                              response.json()["risk_decision"]["reason"])
        # An "exit" declaring an entry intent is contradictory and refused outright.
        contradictory = client.post("/paper/orders", headers=HEADERS, json={
            "client_order_id": "exit-buy", "instrument_id": self.instrument_id, "side": "BUY",
            "quantity": 1, "order_type": "MARKET", "strategy": "manual",
            "intent": "EXIT", "trade_intent": "BUY"})
        self.assertEqual(contradictory.status_code, 422, contradictory.text)
        self.assertIsNone(self.store.orders.get("exit-buy"))
        self.assertEqual(self.store.fills.all(), [])
        self.assertEqual(self.e2e.state(client)["cash"], {"USD": STARTING_CASH})


class LiveSeparationTests(Harness):
    def test_live_route_is_refused_on_a_paper_server(self):
        ctx = self.context()
        self.assertIsNone(ctx.live)
        client = self.client(ctx)
        response = client.post("/live/orders", headers=dict(
            HEADERS, **{"X-Live-Confirm": "anything"}), json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["X-Trading-Mode"], "PAPER")
        self.assertEqual(self.store.orders.all(), [])

    def test_default_and_partial_configuration_never_enable_live(self):
        self.assertIs(load_settings({}).execution.mode, TradingMode.PAPER)
        with self.assertRaisesRegex(ConfigurationError, "only allowed when APP_ENV=production"):
            load_settings({"TRADING_MODE": "live"})

    def test_live_settings_reject_research_only_unknown_and_routed_providers(self):
        base = {"APP_ENV": "production", "TRADING_MODE": "live", "MARKETS": "US,DE",
                "BROKER": "alpaca", "DATABASE_URL": "postgresql://db/trading"}
        for extra, label in (
                ({"DATA_PROVIDER": "mock"}, "DATA_PROVIDER=mock is research-only"),
                ({"DATA_PROVIDER": "yahoo"}, "DATA_PROVIDER=yahoo is research-only"),
                ({"DATA_PROVIDER": "twelvedata"}, "DATA_PROVIDER=twelvedata is research-only"),
                ({"DATA_PROVIDER": "polygon"}, "not a supported provider"),
                ({"DATA_PROVIDER_DE": "twelvedata"}, "DATA_PROVIDER_DE=twelvedata is research-only")):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigurationError) as caught:
                    load_settings(dict(base, **extra))
                self.assertIn(label, str(caught.exception))

    def test_readiness_fails_closed_for_every_non_live_provider(self):
        store = SimpleNamespace(audit=SimpleNamespace(append=lambda *a, **k: None))
        health = SimpleNamespace(data_is_stale=lambda: False)
        for provider, routes in (("mock", ()), ("yahoo", ()), ("twelvedata", ()),
                                 ("polygon", ()), ("future_live_feed", ()),
                                 ("future_live_feed", (("DE", "twelvedata"),))):
            with self.subTest(provider=provider, routes=routes):
                settings = SimpleNamespace(data_provider=provider, data_provider_routes=routes,
                                           base_currency="USD", broker_account_id=None)
                report = LiveReadinessChecker(
                    settings=settings, broker=None, store=store, health=health, recovery=None,
                    gate=None, risk_engine=None, risk_limits=None, registry=None,
                    live_strategies=(), clock_offset=lambda: 0.0, clock=lambda: AS_OF).run()
                self.assertFalse(report.ready)
                check = {c.name: c for c in report.checks}["market_data_healthy"]
                self.assertFalse(check.passed)
                self.assertIn("not approved for live trading", check.detail)


if __name__ == "__main__":
    unittest.main()
