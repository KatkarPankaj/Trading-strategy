import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from stockmarket.dashboard import app as dashboard_app
from stockmarket.dashboard.client import ApiError


def proposal():
    return {
        "proposal_id": "proposal-abc123",
        "rank": 1,
        "symbol": "AAPL",
        "market": "US",
        "side": "BUY",
        "strategy": "orb_vwap",
        "opportunity_score": 75.0,
        "aggregate_score": 0.5,
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "as_of": "2026-10-07T14:00:00+00:00",
        "regime": {"label": "TRENDING_UP"},
        "strategy_selection": {"summary": "test"},
        "research_evidence": [],
        "research_warnings": [],
        "explanation": ["Aggregate action is BUY."],
        "risk_status": "NOT_EVALUATED",
    }


class OpportunityDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get.return_value = [{
            "instrument_id": "XNAS:AAPL",
            "symbol": "AAPL",
            "market": "US",
        }]

    def ui(self, mock_ui, *, submissions):
        mock_ui.form.return_value = nullcontext()
        mock_ui.expander.return_value = nullcontext()
        mock_ui.multiselect.return_value = [
            "AAPL (US · XNAS:AAPL)",
        ]
        mock_ui.form_submit_button.side_effect = submissions
        mock_ui.text_input.return_value = "research-operator"
        mock_ui.number_input.return_value = 2
        mock_ui.session_state = {}

    @patch.object(dashboard_app, "st")
    def test_ranks_and_displays_timestamped_opportunities(self, ui):
        self.ui(ui, submissions=[True, False])
        self.client.post.return_value = {
            "trading_mode": "PAPER",
            "execution": "NOT_SUBMITTED",
            "as_of": "2026-10-07T14:00:00+00:00",
            "proposals": [proposal()],
            "assessments": [],
        }

        dashboard_app._opportunities(self.client)

        self.client.post.assert_called_once()
        path, payload = self.client.post.call_args.args
        self.assertEqual(path, "/intelligence/opportunities")
        self.assertEqual(payload["instrument_ids"], ["XNAS:AAPL"])
        self.assertTrue(payload["as_of"].endswith("+00:00"))
        self.assertEqual(ui.session_state["market_intelligence_result"]["execution"],
                         "NOT_SUBMITTED")
        ui.dataframe.assert_called_once()

    @patch.object(dashboard_app, "st")
    def test_explicit_acceptance_requires_paper_health_and_supplies_quantity(self, ui):
        self.ui(ui, submissions=[True, True])
        self.client.post.side_effect = [
            {
                "trading_mode": "PAPER",
                "execution": "NOT_SUBMITTED",
                "as_of": "2026-10-07T14:00:00+00:00",
                "proposals": [proposal()],
                "assessments": [],
            },
            {
                "trading_mode": "PAPER",
                "execution": "PAPER_ORDER_CREATED",
                "risk_decision": {"status": "APPROVED"},
                "order": {"status": "FILLED"},
            },
        ]
        self.client.get.side_effect = [
            [{"instrument_id": "XNAS:AAPL", "symbol": "AAPL", "market": "US"}],
            {"trading_mode": "PAPER"},
        ]

        dashboard_app._opportunities(self.client)

        self.assertEqual(self.client.post.call_count, 2)
        path, payload = self.client.post.call_args.args
        self.assertEqual(
            path, "/intelligence/proposals/proposal-abc123/submit")
        self.assertEqual(payload, {
            "operator": "research-operator",
            "quantity": 2,
        })
        ui.success.assert_called_once()

    @patch.object(dashboard_app, "st")
    def test_live_api_mode_blocks_proposal_submission(self, ui):
        self.ui(ui, submissions=[True, True])
        self.client.post.return_value = {
            "trading_mode": "PAPER",
            "execution": "NOT_SUBMITTED",
            "as_of": "2026-10-07T14:00:00+00:00",
            "proposals": [proposal()],
            "assessments": [],
        }
        self.client.get.side_effect = [
            [{"instrument_id": "XNAS:AAPL", "symbol": "AAPL", "market": "US"}],
            {"trading_mode": "LIVE"},
        ]

        dashboard_app._opportunities(self.client)

        self.assertEqual(self.client.post.call_count, 1)
        ui.error.assert_called_once_with(
            "Proposal submission is disabled unless the API confirms PAPER mode.")

    @patch.object(dashboard_app, "st")
    def test_order_review_reads_server_order_state(self, ui):
        ui.checkbox.return_value = False
        self.client.get.return_value = [{
            "mode": "PAPER",
            "symbol": "AAPL",
            "status": "FILLED",
        }]

        dashboard_app._orders(self.client)

        self.client.get.assert_called_once_with("/orders", open_only=False)
        ui.dataframe.assert_called_once()

    @patch.object(dashboard_app, "st")
    def test_stale_submission_error_is_shown_to_operator(self, ui):
        self.ui(ui, submissions=[True, True])
        self.client.post.side_effect = [
            {
                "trading_mode": "PAPER",
                "execution": "NOT_SUBMITTED",
                "as_of": "2026-10-07T14:00:00+00:00",
                "proposals": [proposal()],
                "assessments": [],
            },
            ApiError("Proposal submission was rejected as stale (409)", 409),
        ]
        self.client.get.side_effect = [
            [{"instrument_id": "XNAS:AAPL", "symbol": "AAPL", "market": "US"}],
            {"trading_mode": "PAPER"},
        ]

        dashboard_app._opportunities(self.client)

        ui.error.assert_called_once_with(
            "Proposal submission was rejected as stale (409)")


if __name__ == "__main__":
    unittest.main()
