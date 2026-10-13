from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import Mock, patch

from stockmarket.dashboard import app, views
from stockmarket.dashboard.client import ApiError


def cycle():
    return {
        "run_id": "cycle-1", "status": "COMPLETE", "as_of": "2026-10-13T16:30:00+00:00",
        "payload": {"research_run_id": "research-1"}, "events": [],
        "candidates": [{
            "candidate_id": "ARCX:SPY", "status": "NON_ACTIONABLE",
            "payload": {
                "signal": {"signal": {
                    "intent": "NO_TRADE", "strategy": "orb_vwap",
                    "reasons": ["NO_CONFIRMED_BREAKOUT"],
                }},
                "risk": {"status": "NON_ACTIONABLE", "reason": "NO_SIGNAL"},
            },
        }],
    }


def research():
    return {
        "payload": {"request": {"strategy_catalog": [{"name": "orb_vwap"}]}},
        "candidates": [{
            "instrument_id": "ARCX:SPY", "snapshot_id": "snapshot-1",
            "stage": "STRATEGY_SELECTED",
            "opportunity": {"assessment": {
                "recommended_strategy": "orb_vwap", "evidence_quality": "LIMITED",
            }},
        }],
    }


class PaperActivityDashboardTests(TestCase):
    @patch.object(app, "st")
    def test_loads_selected_persisted_cycle_and_research_without_submission(self, ui):
        ui.sidebar.selectbox.return_value = "cycle-1"
        client = Mock()
        client.get.side_effect = [
            {"state": "RUNNING"}, [cycle()], cycle(), research(),
        ]
        activity = app._paper_activity(client)
        self.assertEqual(activity["research"], research())
        self.assertEqual(client.get.call_args_list[1].kwargs, {"limit": 10})
        self.assertEqual(client.get.call_args_list[-1].args, ("/research/autonomous/research-1",))
        client.post.assert_not_called()

    @patch.object(app, "st")
    def test_disabled_loop_does_not_require_cycle_service(self, ui):
        client = Mock()
        client.get.return_value = {"state": "DISABLED"}
        self.assertIsNone(app._paper_activity(client)["cycle"])
        client.get.assert_called_once_with("/paper/loop")

    @patch.object(app, "st")
    def test_defaults_to_completed_history_while_latest_cycle_is_running(self, ui):
        ui.sidebar.selectbox.return_value = "cycle-1"
        active = {**cycle(), "run_id": "cycle-2", "status": "RUNNING"}
        client = Mock()
        client.get.side_effect = [{"state": "RUNNING"}, [active, cycle()], cycle(), research()]
        app._paper_activity(client)
        self.assertEqual(ui.sidebar.selectbox.call_args.kwargs["index"], 1)

    @patch.object(app, "st")
    def test_load_error_is_visible_not_a_successful_cycle(self, ui):
        client = Mock()
        client.get.side_effect = ApiError("API rejected the token (401)", 401)
        result = app._paper_activity(client)
        self.assertIsNone(result["loop"])
        self.assertIsNone(result["cycle"])
        ui.warning.assert_called_once_with("API rejected the token (401)")

    def test_non_actionable_candidate_retains_intent_and_strategy_reasons(self):
        row = views.cycle_candidates(cycle()["candidates"]).iloc[0]
        self.assertEqual(row["intent"], "NO_TRADE")
        self.assertEqual(row["strategy_reasons"], "NO_CONFIRMED_BREAKOUT")
        self.assertEqual(row["reason"], "NO_SIGNAL")
        self.assertEqual(row["order_status"], "NOT_SUBMITTED")

    def test_failed_candidate_has_no_invented_signal_or_approval(self):
        row = views.cycle_candidates([{
            "candidate_id": "XNAS:AAPL", "status": "FAILED",
            "payload": {"error": "STALE_DATA"},
        }]).iloc[0]
        self.assertEqual(row["intent"], "NOT_GENERATED")
        self.assertEqual(row["risk_status"], "NOT_EVALUATED")
        self.assertEqual(row["reason"], "STALE_DATA")

    @patch.object(app, "st")
    def test_news_shows_existing_cycle_assessment_and_snapshot_evidence(self, ui):
        ui.expander.return_value = nullcontext()
        client = Mock()
        snapshot = {"snapshot": {"status": "COMPLETE"}, "evidence": [{"headline": "Fixture news"}]}
        client.get.side_effect = [snapshot, [], []]
        app._news(client, {"research": research()})
        ui.json.assert_any_call(research()["candidates"][0]["opportunity"])
        ui.json.assert_any_call(snapshot)
        client.get.assert_any_call("/research/snapshots/snapshot-1")
        client.post.assert_not_called()

    @patch.object(app, "st")
    def test_strategies_displays_catalog_and_current_selection_without_trades(self, ui):
        client = Mock()
        client.get.side_effect = [[], {"trades": {"count": 0}}]
        app._strategies(client, {"research": research()})
        frames = [call.args[0] for call in ui.dataframe.call_args_list]
        self.assertEqual(frames[0].iloc[0]["name"], "orb_vwap")
        self.assertEqual(frames[1].iloc[0]["strategy"], "orb_vwap")
        client.post.assert_not_called()

    @patch.object(app, "st")
    def test_cycle_completion_is_explained_without_claiming_a_fill(self, ui):
        ui.expander.return_value = nullcontext()
        app._cycle_outcomes({"cycle": cycle()})
        self.assertIn("not that a trade was placed", ui.caption.call_args_list[0].args[0])
        ui.success.assert_not_called()

    @patch.object(app, "st")
    def test_interrupted_cycle_failure_is_explicitly_visible(self, ui):
        ui.expander.return_value = nullcontext()
        interrupted = cycle()
        interrupted["status"] = "PARTIAL"
        interrupted["payload"]["failure"] = "IdempotencyConflict: changed research request"
        app._cycle_outcomes({"cycle": interrupted})
        ui.error.assert_called_once_with("IdempotencyConflict: changed research request")

    @patch.object(app, "st")
    def test_order_review_separates_orders_and_fills_without_execution(self, ui):
        client = Mock()
        client.get.side_effect = [[], {"trading_mode": "PAPER", "fills": []}]
        app._orders(client)
        client.get.assert_any_call("/paper/fills", limit=100)
        ui.subheader.assert_any_call("Actual simulated paper fills")
        client.post.assert_not_called()
