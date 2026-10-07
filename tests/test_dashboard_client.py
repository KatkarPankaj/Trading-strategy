import unittest
from unittest.mock import Mock, patch

import requests

from stockmarket.core.security import Secret
from stockmarket.dashboard.client import ApiClient, ApiError


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ApiClient(
            "http://127.0.0.1:8000", Secret("dashboard-test-token"))

    @staticmethod
    def response(status_code=200, payload=None):
        response = Mock()
        response.status_code = status_code
        response.ok = 200 <= status_code < 400
        response.json.return_value = payload if payload is not None else {"ok": True}
        return response

    @patch("stockmarket.dashboard.client.requests.get")
    def test_get_sends_bearer_token(self, get):
        get.return_value = self.response()

        result = self.client.get("/instruments")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer dashboard-test-token",
        )

    @patch("stockmarket.dashboard.client.requests.post")
    def test_post_sends_authenticated_json(self, post):
        post.return_value = self.response(payload={"status": "COMPLETE"})
        payload = {"instrument_id": "XNAS:AAPL"}

        result = self.client.post("/research", payload)

        self.assertEqual(result, {"status": "COMPLETE"})
        self.assertEqual(post.call_args.kwargs["json"], payload)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer dashboard-test-token",
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Content-Type"], "application/json")

    @patch("stockmarket.dashboard.client.requests.get")
    def test_unauthorized_response_is_reported_without_echoing_credentials(self, get):
        get.return_value = self.response(status_code=401)

        with self.assertRaises(ApiError) as raised:
            self.client.get("/instruments")

        self.assertEqual(raised.exception.status, 401)
        self.assertNotIn("dashboard-test-token", str(raised.exception))

    @patch("stockmarket.dashboard.client.requests.post")
    def test_research_unavailable_explains_server_configuration(self, post):
        post.return_value = self.response(status_code=503)

        with self.assertRaises(ApiError) as raised:
            self.client.post("/research", {})

        self.assertEqual(raised.exception.status, 503)
        self.assertIn("research pipeline", str(raised.exception))
        self.assertIn("market-session", str(raised.exception))

    @patch("stockmarket.dashboard.client.requests.get")
    def test_health_503_is_returned_as_a_health_report(self, get):
        get.return_value = self.response(
            status_code=503, payload={"status": "DOWN"})

        self.assertEqual(
            self.client.get("/health"), {"status": "DOWN"})

    @patch("stockmarket.dashboard.client.requests.post")
    def test_network_failure_is_reported_without_request_details(self, post):
        post.side_effect = requests.ConnectionError("private connection detail")

        with self.assertRaises(ApiError) as raised:
            self.client.post("/research", {})

        self.assertIn("API unreachable: ConnectionError", str(raised.exception))
        self.assertNotIn("private connection detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
