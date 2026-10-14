from unittest import TestCase
from unittest.mock import Mock, patch

from stockmarket.core.api_contract import (
    DASHBOARD_CONTRACT_VERSION, DASHBOARD_FEATURES, dashboard_compatibility_problem,
)
from stockmarket.dashboard import app as dashboard
from stockmarket.dashboard.client import ApiClient, ApiError


def compatible_health():
    return {
        "trading_mode": "PAPER",
        "dashboard_contract": {
            "version": DASHBOARD_CONTRACT_VERSION, "features": list(DASHBOARD_FEATURES)},
    }


class DashboardCompatibilityTests(TestCase):
    def test_compatible_contract_is_not_trading_approval(self):
        health = compatible_health()
        health["status"] = "DEGRADED"
        health["paper_readiness"] = {"status": "BLOCKED"}
        self.assertIsNone(dashboard_compatibility_problem(health))
        self.assertEqual(health["paper_readiness"]["status"], "BLOCKED")

    def test_old_future_malformed_and_missing_capabilities_are_rejected(self):
        for contract in (
            None, {}, {"version": True, "features": list(DASHBOARD_FEATURES)},
            {"version": 2, "features": list(DASHBOARD_FEATURES)},
            {"version": 1, "features": []}, {"version": 1, "features": "market_watch"},
            {"version": 1, "features": [None]},
        ):
            with self.subTest(contract=contract):
                self.assertIsNotNone(dashboard_compatibility_problem({"dashboard_contract": contract}))
        self.assertIsNotNone(dashboard_compatibility_problem(None))

    @patch.object(dashboard, "st")
    @patch.object(dashboard.ApiClient, "from_env")
    def test_old_api_shows_read_only_diagnostics_without_rendering_controls(self, factory, ui):
        client = factory.return_value
        report = {"trading_mode": "PAPER", "status": "DEGRADED"}
        client.get.return_value = report
        ui.sidebar.button.return_value = False
        dashboard.main()
        client.get.assert_called_once_with("/health")
        client.post.assert_not_called()
        ui.tabs.assert_not_called()
        ui.sidebar.radio.assert_not_called()
        ui.json.assert_called_once_with(report)
        self.assertIn("version mismatch", ui.error.call_args.args[0])
        self.assertIn("does NOT pause", ui.warning.call_args.args[0])

    @patch.object(dashboard, "st")
    @patch.object(dashboard.ApiClient, "from_env")
    def test_unreachable_api_never_claims_readiness(self, factory, ui):
        factory.return_value.get.side_effect = ApiError("API unreachable")
        ui.sidebar.button.return_value = False
        dashboard.main()
        ui.tabs.assert_not_called()
        ui.json.assert_not_called()
        factory.return_value.post.assert_not_called()

    def test_missing_market_routes_explain_version_mismatch_without_secrets(self):
        for path in ("/market-watch/markets", "/paper/market-readiness"):
            response = Mock(status_code=404, ok=False)
            response.json.return_value = {"detail": "private internal value"}
            with self.subTest(path=path), self.assertRaisesRegex(ApiError, "capability mismatch") as raised:
                ApiClient._response_json(response, path)
            self.assertNotIn("private internal", str(raised.exception))
            self.assertEqual(raised.exception.status, 404)
