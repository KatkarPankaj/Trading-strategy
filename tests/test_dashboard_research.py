import unittest
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import Mock, patch

from stockmarket.dashboard import app as dashboard_app
from stockmarket.dashboard.client import ApiError


class ResearchDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get.return_value = [{
            "instrument_id": "XNAS:AAPL",
            "symbol": "AAPL",
            "market": "US",
        }]

    @patch.object(dashboard_app, "st")
    def test_research_submits_only_advisory_evidence_request(self, ui):
        ui.form.return_value = nullcontext()
        ui.expander.return_value = nullcontext()
        ui.selectbox.return_value = "AAPL (US · XNAS:AAPL)"
        ui.multiselect.return_value = ["news"]
        ui.slider.return_value = 0.4
        ui.form_submit_button.return_value = True
        self.client.post.return_value = {
            "status": "COMPLETE",
            "trading_mode": "PAPER",
            "reason": None,
            "research_warnings": [],
            "decision": {
                "action": "HOLD",
                "confidence": 55.0,
                "reason_codes": ["INSUFFICIENT_EVIDENCE"],
                "explanation": {},
            },
            "selection": None,
            "signal": None,
            "regime": None,
            "research_evidence": [],
        }

        dashboard_app._research(self.client)

        self.client.post.assert_called_once()
        path, payload = self.client.post.call_args.args
        self.assertEqual(path, "/research")
        self.assertEqual(payload["instrument_id"], "XNAS:AAPL")
        self.assertEqual(payload["evidence"][0]["component"], "news")
        self.assertEqual(payload["evidence"][0]["score"], 0.4)
        self.assertEqual(
            payload["evidence"][0]["source"], "dashboard_operator_input")
        self.assertIsNotNone(
            datetime.fromisoformat(
                payload["evidence"][0]["observed_at"]).tzinfo)
        ui.warning.assert_called_once()
        ui.metric.assert_called_once()

    @patch.object(dashboard_app, "st")
    def test_research_api_error_is_visible(self, ui):
        ui.form.return_value = nullcontext()
        ui.expander.return_value = nullcontext()
        ui.selectbox.return_value = "AAPL (US · XNAS:AAPL)"
        ui.multiselect.return_value = []
        ui.form_submit_button.return_value = True
        self.client.post.side_effect = ApiError("Research is unavailable (503)", 503)

        dashboard_app._research(self.client)

        ui.error.assert_called_once_with("Research is unavailable (503)")
        self.assertEqual(self.client.post.call_args.args[0], "/research")


if __name__ == "__main__":
    unittest.main()
