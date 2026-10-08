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
        if response.status_code == 503 and path == "/health":
            return response.json()
        if response.status_code == 503 and path == "/research":
            raise ApiError(
                "Research is unavailable (503); check the server's research pipeline "
                "and market-session configuration.", 503)
        if response.status_code == 503 and path.startswith("/intelligence/"):
            raise ApiError(
                "Market intelligence is unavailable (503); check provider credentials, "
                "news configuration and market-session coverage.", 503)
        if response.status_code == 409 and "/submit" in path:
            raise ApiError(
                "Proposal submission was rejected as stale or conflicting (409); "
                "regenerate and review the proposal.", 409)
        if not response.ok:
            if path.startswith("/research/personal/"):
                try:
                    detail = response.json().get("detail", "Request rejected")
                except requests.exceptions.JSONDecodeError:
                    detail = "Server returned a non-JSON error; inspect API logs and persisted run status"
                raise ApiError(
                    f"Personal research error {response.status_code}: {detail}", response.status_code)
            raise ApiError(
                f"API error {response.status_code} for {path}", response.status_code)
        return response.json()
