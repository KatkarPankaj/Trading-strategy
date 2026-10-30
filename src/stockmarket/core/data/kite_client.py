"""Explicit, read-only REST access to Kite Connect with guarded request accounting."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..security import Secret, get_secret, require_https
from .kite_usage import KiteUsageController

_API_BASE = "https://api.kite.trade"
_LOGIN_BASE = "https://kite.trade/connect/login"
_ALLOWED_API_HOSTS = frozenset({"api.kite.trade"})
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class KiteConnectError(RuntimeError):
    """A Kite operation failed without retaining provider response or credential text."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        del request, response, code, message, headers, new_url
        return None


class KiteConnectClient:
    """A dormant-by-default Kite client; it performs no requests during construction."""

    def __init__(
        self,
        api_key: str,
        usage: KiteUsageController,
        *,
        api_secret: Secret | None = None,
        access_token: Secret | None = None,
        access_token_file: Path = Path("secrets/kite_access_token.txt"),
        timeout_seconds: float = 10,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 256 \
                or not api_key.isascii() or ":" in api_key \
                or any(char.isspace() or ord(char) < 33 for char in api_key):
            raise ValueError("KITE_API_KEY must be configured")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) \
                or not 1 <= timeout_seconds <= 60:
            raise ValueError("KITE_REQUEST_TIMEOUT_SECONDS must be in [1, 60]")
        if not isinstance(access_token_file, Path):
            raise TypeError("access_token_file must be a Path")
        if not access_token_file.name:
            raise ValueError("KITE_ACCESS_TOKEN_FILE must name a file")
        if api_secret is not None and not isinstance(api_secret, Secret):
            raise TypeError("api_secret must be a Secret")
        if access_token is not None and (
            not isinstance(access_token, Secret)
            or not access_token.reveal().isascii()
            or any(char.isspace() for char in access_token.reveal())
        ):
            raise ValueError("KITE_ACCESS_TOKEN must be a non-empty opaque token")
        self._api_key = api_key.strip()
        self._api_secret = api_secret
        self._access_token = access_token
        self._access_token_file = access_token_file
        self._usage = usage
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], usage: KiteUsageController,
    ) -> "KiteConnectClient":
        api_key_secret = get_secret("KITE_API_KEY", env, required=False)
        api_key = api_key_secret.reveal() if api_key_secret is not None else ""
        secret = get_secret("KITE_API_SECRET", env, required=False)
        token_file = Path(
            env.get("KITE_ACCESS_TOKEN_FILE") or "secrets/kite_access_token.txt")
        token_env = dict(env)
        if "KITE_ACCESS_TOKEN_FILE" not in env and token_file.is_file():
            token_env["KITE_ACCESS_TOKEN_FILE"] = str(token_file)
        token = get_secret("KITE_ACCESS_TOKEN", token_env, required=False)
        try:
            timeout = float(env.get("KITE_REQUEST_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise ValueError("KITE_REQUEST_TIMEOUT_SECONDS must be a number") from None
        return cls(
            api_key, usage, api_secret=secret, access_token=token,
            access_token_file=token_file, timeout_seconds=timeout)

    def login_url(self) -> str:
        """Return the browser-login URL; the caller must perform the interactive login."""
        return f"{_LOGIN_BASE}?{urlencode({'api_key': self._api_key, 'v': '3'})}"

    def exchange_request_token(self, request_token: str) -> None:
        """Exchange one interactive-login token and atomically save the access token."""
        if self._api_secret is None:
            raise KiteConnectError("KITE_API_SECRET_NOT_CONFIGURED")
        if not isinstance(request_token, str) or not request_token.strip() \
                or len(request_token) > 512 or any(char.isspace() for char in request_token):
            raise ValueError("request_token must be a non-empty opaque token")
        token_value = request_token.strip()
        checksum = hashlib.sha256(
            (self._api_key + token_value + self._api_secret.reveal()).encode("utf-8")
        ).hexdigest()
        body = urlencode({
            "api_key": self._api_key,
            "request_token": token_value,
            "checksum": checksum,
        }).encode("ascii")
        response = self._send(
            method="POST",
            endpoint="/session/token",
            category="authentication",
            purpose="exchange interactive Kite request token",
            body=body,
            essential=True,
        )
        try:
            payload = json.loads(response.decode("utf-8"))
            access_token = payload["data"]["access_token"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise KiteConnectError("KITE_AUTH_RESPONSE_INVALID") from None
        if payload.get("status") != "success" or not isinstance(access_token, str) \
                or not access_token.strip() or any(char.isspace() for char in access_token):
            raise KiteConnectError("KITE_AUTHENTICATION_FAILED")
        self._write_access_token(access_token.strip())
        self._access_token = Secret(access_token.strip())

    def download_instrument_master(self) -> bytes:
        """Download the full provider reference dump; this is one explicitly tracked REST call."""
        if self._access_token is None:
            raise KiteConnectError("KITE_ACCESS_TOKEN_NOT_CONFIGURED")
        return self._send(
            method="GET",
            endpoint="/instruments",
            category="instrument_master",
            purpose="refresh cached Kite instrument reference data",
            body=None,
            essential=False,
        )

    def _send(
        self, *, method: str, endpoint: str, category: str, purpose: str,
        body: bytes | None, essential: bool,
    ) -> bytes:
        url = require_https(_API_BASE + endpoint, allowed_hosts=_ALLOWED_API_HOSTS)
        ticket = self._usage.begin_request(
            category=category, method=method, endpoint=endpoint,
            purpose=purpose, essential=essential)
        headers = {
            "X-Kite-Version": "3",
            "Accept": "text/csv" if endpoint == "/instruments" else "application/json",
        }
        if self._access_token is not None and endpoint != "/session/token":
            headers["Authorization"] = (
                f"token {self._api_key}:{self._access_token.reveal()}")
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=headers, method=method)
        opener = build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                status_code = response.getcode()
                content = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            self._usage.record_response(ticket, error.code)
            raise KiteConnectError(
                "KITE_HTTP_ERROR", status_code=error.code) from None
        except TimeoutError:
            self._usage.record_failure(ticket, "TIMEOUT")
            raise KiteConnectError("KITE_REQUEST_TIMEOUT") from None
        except URLError:
            self._usage.record_failure(ticket, "CONNECTION_ERROR")
            raise KiteConnectError("KITE_CONNECTION_FAILED") from None
        except OSError:
            self._usage.record_failure(ticket, "CONNECTION_ERROR")
            raise KiteConnectError("KITE_CONNECTION_FAILED") from None
        self._usage.record_response(ticket, status_code)
        if status_code != 200:
            raise KiteConnectError("KITE_UNEXPECTED_HTTP_STATUS", status_code=status_code)
        if len(content) > _MAX_RESPONSE_BYTES:
            raise KiteConnectError("KITE_RESPONSE_TOO_LARGE")
        return content

    def _write_access_token(self, value: str) -> None:
        path = self._access_token_file
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".kite-token-", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as token_file:
                token_file.write(value)
                token_file.write("\n")
                token_file.flush()
                os.fsync(token_file.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def __repr__(self) -> str:
        return "KiteConnectClient(***)"
