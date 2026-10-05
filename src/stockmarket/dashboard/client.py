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
        headers = {"Authorization": f"Bearer {self._token.reveal()}"} if self._token else {
        }
        try:
            response = requests.get(f"{self._base}{path}", params=params or None,
                                    headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ApiError(f"API unreachable: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise ApiError("API rejected the token (401)", 401)
        if response.status_code == 503 and path == "/health":
            return response.json()  # DOWN is still a valid health report
        if not response.ok:
            raise ApiError(
                f"API error {response.status_code} for {path}", response.status_code)
        return response.json()
