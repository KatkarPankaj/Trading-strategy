"""Thin HTTP client for the trading API; the dashboard has no other data source."""

from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from ..core.security import Secret, SecurityError, get_secret, require_https

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def validate_base_url(url: str) -> str:
    """HTTPS everywhere except a loopback address used for local development."""
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return url.rstrip("/")
    return require_https(url).rstrip("/")


class ApiClient:
    def __init__(self, base_url: str, token: Secret | None = None, *, timeout: float = 10.0) -> None:
        self._base = validate_base_url(base_url)
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ApiClient":
        env = os.environ if env is None else env
        try:
            token = get_secret("API_TOKEN", env, required=False)
        except SecurityError as exc:
            raise ApiError(str(exc)) from exc
        return cls(env.get("API_BASE_URL", "http://127.0.0.1:8000"), token)

    @classmethod
    def worker_clients_from_env(
        cls, env: Mapping[str, str] | None = None,
    ) -> dict[str, "ApiClient"]:
        """Build explicit market-scoped API clients; never infer execution from Market Watch."""
        env = os.environ if env is None else env
        urls = {
            market: (env.get(f"API_BASE_URL_{market}") or "").strip()
            for market in ("IN", "US")
        }
        configured = {market: url for market, url in urls.items() if url}
        if not configured:
            return {"DEFAULT": cls.from_env(env)}
        try:
            token = get_secret("API_TOKEN", env, required=False)
        except SecurityError as exc:
            raise ApiError(str(exc)) from exc
        clients = {market: cls(url, token) for market, url in configured.items()}
        bases = [client._base for client in clients.values()]
        if len(set(bases)) != len(bases):
            raise ApiError("each paper market must use a distinct API worker endpoint")
        return clients

    @staticmethod
    def worker_market_problem(expected_market: str, health: Mapping[str, Any]) -> str | None:
        worker = health.get("paper_worker")
        worker_readiness = health.get("paper_readiness")
        actual_market = (
            worker.get("execution_market") if isinstance(worker, Mapping) else
            worker_readiness.get("execution_market")
            if isinstance(worker_readiness, Mapping) else None
        )
        if actual_market != expected_market:
            return (
                f"Configured {expected_market} dashboard endpoint reports execution market "
                f"{actual_market or 'UNKNOWN'}; refusing to show its controls.")
        return None

    def get(self, path: str, **params: Any) -> Any:
        try:
            response = requests.get(
                f"{self._base}{path}",
                params=params or None,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"API unreachable: {type(exc).__name__}") from exc
        return self._response_json(response, path)

    def post(self, path: str, payload: Mapping[str, Any], *, timeout: float | None = None) -> Any:
        if timeout is not None and not 0 < timeout <= 600:
            raise ValueError("request timeout must be between 0 and 600 seconds")
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(
                f"{self._base}{path}",
                json=dict(payload),
                headers=headers,
                timeout=self._timeout if timeout is None else timeout,
            )
        except requests.ReadTimeout as exc:
            raise ApiError(
                "API response timed out; the server may still be processing the request. "
                "No automatic retry was made. Check run/order status before resubmitting."
            ) from exc
        except requests.RequestException as exc:
            raise ApiError(f"API unreachable: {type(exc).__name__}") from exc
        return self._response_json(response, path)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            return {}
        return {"Authorization": f"Bearer {self._token.reveal()}"}

    @staticmethod
    def _response_json(response: requests.Response, path: str) -> Any:
        if response.status_code == 401:
            raise ApiError("API rejected the token (401)", 401)
        if response.status_code == 404 and (
                path.startswith("/market-watch/") or path == "/paper/market-readiness"):
            raise ApiError(
                "Dashboard/API capability mismatch (404): this API lacks the requested Market Watch "
                "or readiness route. Run matching versions after an authorized controlled restart; "
                "do not reinterpret this as an empty market list or enable trading.", 404)
        if response.status_code == 503 and path == "/health":
            return response.json()
        if response.status_code == 503 and path == "/research":
            raise ApiError(
                "Research is unavailable (503); check the server's research pipeline "
                "and market-session configuration.", 503)
        if response.status_code == 503 and path.startswith("/intelligence/") \
                and not path.startswith("/intelligence/prospective/"):
            try:
                payload = response.json()
            except requests.exceptions.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("detail") == \
                    "market intelligence service is not configured":
                raise ApiError(
                    "Market intelligence is not configured (503). This feature requires "
                    "AI research, NEWS_PROVIDER=finnhub and FINNHUB_API_KEY or "
                    "FINNHUB_API_KEY_FILE on the API server, then an API restart. "
                    "The paper auto loop is a separate workflow; check its cycle results.", 503)
            raise ApiError(
                "Market intelligence is unavailable (503); check provider credentials, "
                "news configuration and market-session coverage.", 503)
        if response.status_code == 409 and "/submit" in path:
            raise ApiError(
                "Proposal submission was rejected as stale or conflicting (409); "
                "regenerate and review the proposal.", 409)
        if not response.ok:
            if path.startswith(("/research/personal/", "/strategy-lab/", "/paper/loop/",
                                "/intelligence/prospective/", "/watchlist", "/market-watch/")):
                try:
                    detail = response.json().get("detail", "Request rejected")
                except requests.exceptions.JSONDecodeError:
                    detail = "Server returned a non-JSON error; inspect API logs and persisted run status"
                raise ApiError(
                    f"Request rejected ({response.status_code}): {detail}", response.status_code)
            raise ApiError(
                f"API error {response.status_code} for {path}", response.status_code)
        return response.json()
