"""Deterministic research-to-paper-trading integration test.

The production composition (`build_context`) and HTTP API are exercised end to end with an
injected clock, a controlled research-only market-data fixture and a test-only advisory AI.
No network access, paid API or broker credential is used, and nothing leaves the paper broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import unittest

from fastapi.testclient import TestClient

from stockmarket.api.app import create_app
from stockmarket.api.bootstrap import build_context
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.data.provider import Quote
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.recovery import rebuild_portfolio
from stockmarket.core.settings import ConfigurationError, load_settings
from stockmarket.core.strategies import OrbVwapStrategy

from test_personal_research import AS_OF, AdvisoryAI, ResearchData


TOKEN = "end-to-end-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
STARTING_CASH = 100_000.0
ENV = {
    "APP_ENV": "test", "MARKETS": "US", "BASE_CURRENCY": "USD",
    "API_TOKEN": TOKEN, "DATA_PROVIDER": "mock", "PERSONAL_RESEARCH": "false",
    "INSTRUMENT_BOOTSTRAP": "none", "PAPER_STARTING_CASH": str(STARTING_CASH),
    "ENTRY_WINDOW_START": "09:45", "ENTRY_WINDOW_END": "15:00",
    "MAX_POSITION_QUANTITY": "100", "MAX_ORDER_NOTIONAL": "25000",
    "RESEARCH_SESSIONS": json.dumps({"US": {
        "opening_range_minutes": 15, "entry_cutoff": "15:00",
        "square_off": "15:55", "late_entry_start": "12:00"}}),
}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class ControlledData(ResearchData):
    """Research fixture whose quote price and timestamp are set explicitly by the test."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.price = 102.0
        self.quote_at: datetime | None = None

    def get_quote(self, instrument):
        quote = super().get_quote(instrument)
        return Quote(quote.instrument_id, self.price, self.quote_at or self.clock(), self.name)


def market_stats(_instrument_id):
    return {
        "correlated_exposure": 0.0,
        "average_daily_traded_value": 100_000_000.0,
        "expected_slippage_bps": 5.0,
    }


def make_store() -> Store:
    db = SQLiteDatabase()
    migrate(db)
    return Store(db)


class PaperEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.db.close)
        instrument = default_markets().get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.instrument_id = instrument.instrument_id
        self.store.instruments.save(instrument)
        self.clock = Clock(AS_OF)
        self.data = ControlledData(self.clock)
        self.ai = AdvisoryAI()

    def context(self):
        ctx = build_context(
            load_settings(ENV), ENV, store=self.store,
            market_stats=market_stats, sector_of=lambda _: "TECHNOLOGY",
            research_analyst=AIAnalyst(self.ai, clock=self.clock),
            research_strategies={"orb_vwap": OrbVwapStrategy()},
            clock=self.clock, market_data_provider=self.data,
        )
        self.addCleanup(ctx.market_data.close)
        return ctx

    def client(self, ctx):
        client = TestClient(create_app(ctx))
        self.addCleanup(client.close)
        return client

    def test_finnhub_configuration_builds_market_intelligence_without_network(self):
        env = dict(ENV, NEWS_PROVIDER="finnhub", FINNHUB_API_KEY="fixture-news-key")
        analyst = AIAnalyst(self.ai, clock=self.clock)
        ctx = build_context(
            load_settings(env), env, store=self.store,
            market_stats=market_stats, sector_of=lambda _: "TECHNOLOGY",
            research_analyst=analyst,
            research_strategies={"orb_vwap": OrbVwapStrategy()},
            clock=self.clock, market_data_provider=self.data,
        )
        self.addCleanup(ctx.market_data.close)
        self.assertIsNotNone(ctx.market_intelligence)
        self.assertIsNotNone(ctx.research_pipeline)
        producer = ctx.research_pipeline.news_evidence_producer
        self.assertIs(producer.analyst, analyst)
        self.assertEqual(producer.provider.name, "finnhub")
        self.assertIs(producer.repository, self.store.news)

    # -- helpers driving the real HTTP API ------------------------------------------------

    def run_research(self, client, key="e2e-run"):
        response = client.post("/research/autonomous", headers=HEADERS, json={
            "universe_id": "US_ALL", "idempotency_key": key,
            "as_of": AS_OF.isoformat(), "top_n": 1})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def propose(self, client, key="e2e-run"):
        run = self.run_research(client, key)
        self.assertEqual(run["status"], "COMPLETE")
        self.assertTrue(run["research_only"])
        self.assertEqual(run["execution"], "NOT_SUBMITTED")
        candidate = run["candidates"][0]["instrument_id"]
        self.assertEqual(candidate, self.instrument_id)
        base = f"/research/autonomous/{run['run_id']}/candidates/{candidate}"
        signal = client.post(f"{base}/signal", headers=HEADERS,
                             json={"evaluation_as_of": AS_OF.isoformat()})
        self.assertEqual(signal.status_code, 200, signal.text)
        self.assertEqual(signal.json()["execution"], "NOT_SUBMITTED")
        risk = client.post(f"{base}/risk", headers=HEADERS, json={})
        self.assertEqual(risk.status_code, 200, risk.text)
        proposal = risk.json()
        self.assertEqual(proposal["execution"], "NOT_SUBMITTED")
        return proposal

    def submit(self, client, proposal_id):
        return client.post(f"/intelligence/proposals/{proposal_id}/submit",
                           headers=HEADERS, json={"operator": "e2e-operator"})

    def state(self, client):
        portfolio = client.get("/portfolio", headers=HEADERS).json()
        positions = client.get("/positions", headers=HEADERS).json()["positions"]
        return {
            "orders": [(o["client_order_id"], o["status"], o["filled_quantity"])
                       for o in self.store.orders.all()],
            "fills": [(f["client_order_id"], f["quantity"], f["price"])
                      for f in self.store.fills.all()],
            "positions": [(p["instrument_id"], p["side"], p["quantity"]) for p in positions],
            "cash": portfolio["cash"],
            "realized_pnl": portfolio["realized_pnl"],
        }

    def assert_untouched(self, client, *, audit_orders=("REJECTED", "FAILED")):
        """No broker order, fill, position or cash change; only terminal audit order rows."""
        state = self.state(client)
        self.assertTrue(all(status in audit_orders and filled == 0
                            for _, status, filled in state["orders"]), state["orders"])
        self.assertEqual(self.store.paper_execution.all(), [])
        self.assertEqual(state["fills"], [])
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["cash"], {"USD": STARTING_CASH})

    # -- tests -----------------------------------------------------------------------------

    def test_research_to_paper_round_trip_survives_restart(self):
        client = self.client(self.context())

        unauthenticated = client.post("/research/autonomous", json={
            "universe_id": "US_ALL", "idempotency_key": "no-token"})
        self.assertEqual(unauthenticated.status_code, 401)

        proposal = self.propose(client)
        self.assertEqual(proposal["decision"]["status"], "APPROVED")
        self.assertEqual(proposal["provenance"]["signal_summary"]["intent"], "BUY")
        entry_price = proposal["entry_price"]
        self.assertIsNotNone(self.store.trade_proposals.get_by_id(proposal["proposal_id"]))
        self.assert_untouched(client, audit_orders=())

        self.data.price = entry_price
        submitted = self.submit(client, proposal["proposal_id"])
        self.assertEqual(submitted.status_code, 200, submitted.text)
        order = submitted.json()["order"]
        self.assertEqual((order["status"], order["trade_intent"], order["side"]),
                         ("FILLED", "BUY", "BUY"))
        quantity = order["filled_quantity"]
        self.assertGreater(quantity, 0)
        after_entry = self.state(client)
        self.assertEqual(after_entry["positions"], [(self.instrument_id, "LONG", quantity)])
        self.assertEqual(after_entry["cash"], {"USD": STARTING_CASH - quantity * entry_price})
        self.assertEqual(len(after_entry["fills"]), 1)
        self.assertEqual([p["instrument_id"] for p in self.store.positions.all()],
                         [self.instrument_id])
        self.assertIsNotNone(self.store.pnl.latest())

        retry = self.submit(client, proposal["proposal_id"])
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(self.state(client), after_entry)

        self.clock.at = AS_OF + timedelta(minutes=5)
        self.data.price = 149.9
        monitored = client.post("/paper/positions/manage", headers=HEADERS)
        self.assertEqual(monitored.status_code, 200, monitored.text)
        self.assertEqual(monitored.json()["positions"][0]["status"], "MONITORED")

        self.clock.at = AS_OF + timedelta(minutes=10)
        target = proposal["provenance"]["signal_summary"]["target_price"]
        self.data.price = target + 0.3
        managed = client.post("/paper/positions/manage", headers=HEADERS)
        self.assertEqual(managed.status_code, 200, managed.text)
        exit_outcome = managed.json()["positions"][0]
        self.assertEqual(exit_outcome["trigger_reason"], "TAKE_PROFIT")
        exit_order = exit_outcome["order"]
        self.assertEqual((exit_order["status"], exit_order["trade_intent"], exit_order["side"]),
                         ("FILLED", "SELL_TO_CLOSE", "SELL"))
        expected_pnl = round(quantity * (self.data.price - entry_price), 6)
        final = self.state(client)
        self.assertEqual(final["positions"], [])
        self.assertAlmostEqual(final["realized_pnl"], expected_pnl, places=6)
        self.assertAlmostEqual(final["cash"]["USD"], STARTING_CASH + expected_pnl, places=6)
        self.assertEqual(len(final["orders"]), 2)
        self.assertEqual(len(final["fills"]), 2)
        self.assertEqual(self.store.positions.all(), [])

        repeat_manage = client.post("/paper/positions/manage", headers=HEADERS)
        self.assertEqual(repeat_manage.json()["positions"], [])
        self.assertEqual(self.state(client), final)

        # Restart: a fresh production composition over the same durable store.
        restarted = self.client(self.context())
        self.assertEqual(self.state(restarted), final)
        reconciled = restarted.post("/recovery/reconcile", headers=HEADERS)
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        self.assertTrue(reconciled.json()["clean"], reconciled.json())
        retry_after_restart = self.submit(restarted, proposal["proposal_id"])
        self.assertEqual(retry_after_restart.status_code, 200, retry_after_restart.text)
        self.assertTrue(retry_after_restart.json()["duplicate"])
        self.assertEqual(self.state(restarted), final)

    def test_restart_with_open_position_restores_and_exits(self):
        client = self.client(self.context())
        proposal = self.propose(client)
        self.data.price = proposal["entry_price"]
        self.assertEqual(self.submit(client, proposal["proposal_id"]).status_code, 200)
        before = self.state(client)

        restarted = self.client(self.context())
        self.assertEqual(self.state(restarted), before)
        self.assertTrue(restarted.post("/recovery/reconcile", headers=HEADERS).json()["clean"])

        self.clock.at = AS_OF + timedelta(minutes=5)
        self.data.price = proposal["provenance"]["signal_summary"]["stop_price"] - 0.1
        managed = restarted.post("/paper/positions/manage", headers=HEADERS)
        self.assertEqual(managed.status_code, 200, managed.text)
        outcome = managed.json()["positions"][0]
        self.assertEqual(outcome["trigger_reason"], "STOP_LOSS")
        self.assertEqual(outcome["order"]["trade_intent"], "SELL_TO_CLOSE")
        final = self.state(restarted)
        self.assertEqual(final["positions"], [])
        self.assertLess(final["realized_pnl"], 0)

    def test_stale_quote_at_submission_creates_no_order(self):
        client = self.client(self.context())
        proposal = self.propose(client)
        self.data.price = proposal["entry_price"]
        self.data.quote_at = AS_OF - timedelta(hours=1)
        response = self.submit(client, proposal["proposal_id"])
        self.assertIn(response.status_code, {409, 422}, response.text)
        self.assert_untouched(client)

    def test_risk_rejection_creates_no_order(self):
        env = dict(ENV, MAX_ORDER_NOTIONAL="100")
        ctx = build_context(
            load_settings(env), env, store=self.store,
            market_stats=market_stats, sector_of=lambda _: "TECHNOLOGY",
            research_analyst=AIAnalyst(self.ai, clock=self.clock),
            research_strategies={"orb_vwap": OrbVwapStrategy()},
            clock=self.clock, market_data_provider=self.data,
        )
        self.addCleanup(ctx.market_data.close)
        client = self.client(ctx)
        proposal = self.propose(client)
        self.assertEqual(proposal["decision"]["status"], "REJECTED")
        self.assertIsNone(proposal["proposal_id"])
        self.data.price = proposal["entry_price"]
        response = self.submit(client, "not-an-approved-proposal")
        self.assertEqual(response.status_code, 404, response.text)
        self.assert_untouched(client, audit_orders=())

    def test_duplicate_research_request_with_different_payload_conflicts(self):
        client = self.client(self.context())
        first = self.run_research(client, "dup-key")
        again = self.run_research(client, "dup-key")
        self.assertEqual(again["run_id"], first["run_id"])
        conflict = client.post("/research/autonomous", headers=HEADERS, json={
            "universe_id": "US_ALL", "idempotency_key": "dup-key",
            "as_of": (AS_OF - timedelta(minutes=5)).isoformat(), "top_n": 1})
        self.assertEqual(conflict.status_code, 422, conflict.text)
        self.assertIn("idempotency_key", conflict.text)
        self.assert_untouched(client, audit_orders=())

    def test_interrupted_fill_write_rolls_back_and_retry_is_idempotent(self):
        client = self.client(self.context())
        proposal = self.propose(client)
        self.data.price = proposal["entry_price"]
        original_save = self.store.fills.save

        def interrupted(*_args, **_kwargs):
            raise RuntimeError("simulated durable fill write failure")

        self.store.fills.save = interrupted
        try:
            failed = self.submit(client, proposal["proposal_id"])
        finally:
            self.store.fills.save = original_save
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["execution"], "PAPER_ORDER_FAILED")
        self.assertIn("SUBMIT_FAILED_STATE_UNKNOWN", failed.json()["order"]["error"])
        self.assert_untouched(client)

        restarted = self.client(self.context())
        # Reconciliation proves the durable executor never accepted the order and
        # authorizes exactly one safe retry with the same client order id.
        reconciled = restarted.post("/recovery/reconcile", headers=HEADERS)
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        retry = self.submit(restarted, proposal["proposal_id"])
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["execution"], "PAPER_ORDER_CREATED")
        self.assertEqual(retry.json()["order"]["status"], "FILLED")
        after_retry = self.state(restarted)
        self.assertEqual(len(after_retry["orders"]), 1)
        self.assertEqual(len(after_retry["fills"]), 1)
        self.assertEqual(len(self.store.paper_execution.all()), 1)
        self.assertIsNotNone(self.store.pnl.latest())

        again = self.submit(self.client(self.context()), proposal["proposal_id"])
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["duplicate"])
        self.assertEqual(self.state(restarted), after_retry)

    def test_kill_switch_blocks_entry_without_mutation(self):
        client = self.client(self.context())
        proposal = self.propose(client)
        self.data.price = proposal["entry_price"]
        triggered = client.post("/kill-switch/trigger", headers=HEADERS, json={
            "operator": "e2e-operator", "reason": "integration test halt"})
        self.assertEqual(triggered.status_code, 200, triggered.text)
        blocked = self.submit(client, proposal["proposal_id"])
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["execution"], "PAPER_ORDER_REJECTED")
        self.assertIn("KILL_SWITCH", blocked.json()["order"]["error"])
        self.assert_untouched(client, audit_orders=("REJECTED",))

    # -- opt-in paper auto loop ---------------------------------------------------------

    def loop_context(self, **overrides):
        env = dict(ENV, **overrides)
        ctx = build_context(
            load_settings(env), env, store=self.store,
            market_stats=market_stats, sector_of=lambda _: "TECHNOLOGY",
            research_analyst=AIAnalyst(self.ai, clock=self.clock),
            research_strategies={"orb_vwap": OrbVwapStrategy()},
            clock=self.clock, market_data_provider=self.data,
        )
        self.addCleanup(ctx.market_data.close)
        return ctx

    def test_paper_loop_is_disabled_by_default(self):
        ctx = self.context()
        self.assertIsNone(ctx.paper_loop)
        response = self.client(ctx).get("/paper/loop", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "DISABLED")

    def test_paper_loop_is_built_from_env_but_only_started_with_the_app(self):
        ctx = self.loop_context(PAPER_AUTO_LOOP="true", PAPER_AUTO_LOOP_TOP_N="2",
                                PAPER_AUTO_LOOP_INTERVAL_MINUTES="30")
        status = ctx.paper_loop.status()
        self.assertEqual((status["state"], status["trading_mode"]), ("IDLE", "PAPER"))
        self.assertEqual((status["universe_id"], status["market"]), ("US_ALL", "US"))
        self.assertEqual((status["top_n"], status["interval_minutes"]), (2, 30))

        client = self.client(ctx)
        self.assertEqual(client.get("/paper/loop").status_code, 401)
        self.assertEqual(client.get("/paper/loop", headers=HEADERS).json()["state"], "IDLE")

        started, stopped = [], []
        ctx.paper_loop.start = lambda: started.append(True)
        ctx.paper_loop.stop = lambda: stopped.append(True)
        with TestClient(create_app(ctx)):
            self.assertEqual(started, [True])
        self.assertEqual(stopped, [True])

    def test_polling_loop_does_not_use_the_continuous_feed_staleness_trigger(self):
        self.assertTrue(self.context().markets.is_regular_session("US", self.clock()))
        codes = {code for code, _ in self.context().monitor.evaluate()}
        self.assertIn("STALE_MARKET_DATA", codes)
        loop_codes = {code for code, _ in self.loop_context(PAPER_AUTO_LOOP="true").monitor.evaluate()}
        self.assertNotIn("STALE_MARKET_DATA", loop_codes)

    def test_unsafe_paper_loop_configuration_fails_closed(self):
        for overrides in ({"PAPER_AUTO_LOOP": "yes"},
                          {"PAPER_AUTO_LOOP": "true", "PAPER_AUTO_LOOP_MARKET": "IN"},
                          {"PAPER_AUTO_LOOP": "true", "PAPER_AUTO_LOOP_INTERVAL_MINUTES": "0"},
                          {"PAPER_AUTO_LOOP": "true", "PAPER_AUTO_LOOP_TOP_N": "many"}):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigurationError):
                self.loop_context(**overrides)


if __name__ == "__main__":
    unittest.main()