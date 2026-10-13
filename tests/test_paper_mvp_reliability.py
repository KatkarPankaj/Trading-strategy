from datetime import timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

import test_paper_end_to_end as fixtures
from stockmarket.core.models import OrderStatus
from stockmarket.core.brokers import BrokerError


class PaperMvpReliabilityTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.PaperEndToEndTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_automatic_entry_exit_collects_immutable_evidence_and_restart_replays_once(self):
        f = self.fixture
        ctx = f.context(auto_loop=True)
        client = f.client(ctx)
        self.assertEqual(ctx.paper_loop.tick()["action"], "CYCLE")
        decision = f.store.prospective.decisions()[0]
        self.assertEqual(ctx.prospective.observation(decision["decision_id"])["status"],
                         "FILLED_AWAITING_CLOSED_OUTCOME")
        f.clock.at += timedelta(minutes=1)
        f.data.price = 100
        monitored = ctx.paper_loop.tick()
        self.assertEqual(monitored["action"], "MONITOR")
        self.assertEqual(monitored["result"]["positions"][0]["trigger_reason"], "STOP_LOSS")
        observation = ctx.prospective.observation(decision["decision_id"])
        self.assertEqual(observation["status"], "LABELED_CLOSED_EPISODE")
        self.assertAlmostEqual(observation["labels"][0]["net_return"], -2 / 102)
        self.assertEqual(f.store.prospective.decision(decision["decision_id"]), decision)
        self.assertEqual(len(f.store.orders.all()), 2)
        self.assertEqual(len(f.store.paper_outcomes.recent()), 1)
        final = f.state(client)
        restarted = f.context(auto_loop=True)
        self.assertEqual(f.state(f.client(restarted)), final)
        self.assertEqual(restarted.prospective.reconcile()["attachments_added"], 0)
        self.assertEqual(restarted.prospective.observation(decision["decision_id"]), observation)
        report = restarted.prospective.readiness()
        self.assertEqual((report["status"], report["training"]), ("NOT_READY", "NOT_PERFORMED"))

    def test_entry_pause_requires_auth_is_idempotent_persists_and_does_not_clear_kill(self):
        f = self.fixture
        ctx = f.context(auto_loop=True)
        client = f.client(ctx)
        path = "/paper/loop/control"
        body = {"entries_enabled": False, "operator": "operator", "reason": "maintenance"}
        self.assertEqual(client.post(path, json=body).status_code, 401)
        invalid = dict(body, entries_enabled="false")
        self.assertEqual(client.post(path, headers=fixtures.HEADERS, json=invalid).status_code, 422)
        for _ in range(2):
            response = client.post(path, headers=fixtures.HEADERS, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["entries_enabled"])
        audits = f.store.audit.entries("automatic-entries")
        self.assertEqual(len(audits), 1)
        restarted = f.context(auto_loop=True)
        self.assertFalse(restarted.paper_loop.status()["operator_entries_enabled"])
        self.assertEqual(restarted.paper_loop.recover_unfinished(), [])
        self.assertEqual(restarted.paper_loop.tick()["action"], "EXITS_ONLY")
        self.assertEqual(f.store.orders.all(), [])
        restarted.kill_switch.trigger(
            "test", "risk gate remains authoritative", source="manual", actor="operator")
        new_client = f.client(restarted)
        response = new_client.post(path, headers=fixtures.HEADERS, json=dict(body, entries_enabled=True))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(restarted.paper_loop.gate.halted)
        self.assertEqual(restarted.paper_loop.tick()["action"], "EXITS_ONLY")
        self.assertEqual(f.store.orders.all(), [])

    def test_pending_partial_entry_completes_before_stop_monitoring(self):
        f = self.fixture
        ctx = f.context()
        client = f.client(ctx)
        proposal = f.propose(client)
        broker = ctx.recovery._broker
        original_match = broker._apply_match

        def bounded_match(record, *, persist):
            quantity = record.quantity
            record.quantity = min(quantity, record.filled + 40)
            try:
                original_match(record, persist=False)
            finally:
                record.quantity = quantity
            if record.status is OrderStatus.FILLED and record.filled < quantity:
                record.status = OrderStatus.PARTIALLY_FILLED
            if persist:
                broker._save_rec(record)

        with patch.object(broker, "_apply_match", side_effect=bounded_match):
            self.assertEqual(f.submit(client, proposal["proposal_id"]).status_code, 200)
        self.assertEqual(f.store.orders.all()[0]["status"], "PARTIALLY_FILLED")
        with patch.object(ctx.paper, "cancel", side_effect=BrokerError("cancellation unavailable")):
            failed_flatten = ctx.paper_position_manager.flatten(market="US")
        self.assertEqual(failed_flatten["orders"][0]["status"], "RECONCILE_FAILED")
        self.assertEqual(failed_flatten["positions"][0]["status"], "ENTRY_ORDER_PENDING")
        self.assertEqual(f.store.orders.all()[0]["filled_quantity"], 40)
        self.assertEqual(len(f.state(client)["fills"]), 1)
        result = ctx.paper_position_manager.manage()
        self.assertEqual(result["orders"][0]["status"], "FILLED")
        self.assertEqual(result["positions"][0]["status"], "MONITORED")
        self.assertEqual(f.store.orders.all()[0]["filled_quantity"], 100)
        f.clock.at += timedelta(minutes=1)
        f.data.price = 100
        self.assertEqual(ctx.paper_position_manager.manage()["positions"][0]["trigger_reason"], "STOP_LOSS")
        self.assertEqual(f.state(client)["positions"], [])
        self.assertEqual(len(f.store.paper_outcomes.recent()), 1)

    def test_end_of_day_cancels_pending_entry_without_new_fill_or_price(self):
        f = self.fixture
        ctx = f.context()
        client = f.client(ctx)
        proposal = f.propose(client)
        broker = ctx.recovery._broker
        with patch.object(broker, "_apply_match"):
            self.assertEqual(f.submit(client, proposal["proposal_id"]).status_code, 200)
        f.clock.at = f.clock.at.replace(hour=19, minute=52)
        f.data.reject_quotes = True
        result = ctx.paper_position_manager.flatten(market="US")
        self.assertEqual(result["orders"][0]["status"], "CANCELLED")
        self.assertEqual(f.store.orders.all()[0]["status"], "CANCELLED")
        self.assertEqual(f.state(client)["fills"], [])
        self.assertEqual(f.state(client)["positions"], [])
        self.assertEqual(ctx.paper_position_manager.flatten(market="US")["orders"], [])

    def test_pending_exit_reconciles_without_duplicate_exit(self):
        f = self.fixture
        ctx = f.context()
        client = f.client(ctx)
        proposal = f.propose(client)
        self.assertEqual(f.submit(client, proposal["proposal_id"]).status_code, 200)
        f.clock.at += timedelta(minutes=1)
        f.data.price = 100
        with patch.object(ctx.recovery._broker, "_apply_match"):
            ctx.paper_position_manager.manage()
        self.assertEqual(len(f.store.orders.all()), 2)
        self.assertEqual(f.store.orders.all()[-1]["status"], "ACCEPTED")
        result = ctx.paper_position_manager.manage()
        self.assertEqual(result["orders"][0]["status"], "FILLED")
        self.assertEqual(result["positions"], [])
        self.assertEqual(f.state(client)["positions"], [])
        self.assertEqual(len(f.store.orders.all()), 2)
        self.assertEqual(len(f.store.paper_outcomes.recent()), 1)

    def test_health_readiness_is_read_only_closed_and_stale_data_stay_blocked(self):
        f = self.fixture
        ctx = f.context(auto_loop=True)
        client = f.client(ctx)
        f.clock.at = f.clock.at.replace(hour=20, minute=5)
        with patch.object(ctx.prospective, "reconcile", side_effect=AssertionError("GET mutation")):
            response = client.get("/health", headers=fixtures.HEADERS)
        self.assertEqual(response.status_code, 200)
        readiness = response.json()["paper_readiness"]
        self.assertEqual(readiness["status"], "BLOCKED")
        self.assertFalse(readiness["risk_approval"])
        self.assertIn("NO_REGULAR_MARKET_SESSION", readiness["blockers"])
        self.assertIn("MISSING_OR_STALE_EXECUTION_DATA", readiness["blockers"])
        self.assertEqual(f.store.orders.all(), [])

    def test_shutdown_stops_loop_before_provider_and_never_closes_under_active_work(self):
        f = self.fixture
        ctx = f.context(auto_loop=True)
        app = f.client(ctx).app
        ordered = Mock()
        ctx.paper_loop.stop = ordered.stop
        ctx.market_data.close = ordered.close
        app.router.on_shutdown[0]()
        self.assertEqual([call[0] for call in ordered.mock_calls], ["stop", "close"])
        ordered.reset_mock()
        ordered.stop.side_effect = RuntimeError("active worker")
        with self.assertRaisesRegex(RuntimeError, "active worker"):
            app.router.on_shutdown[0]()
        ordered.close.assert_not_called()
