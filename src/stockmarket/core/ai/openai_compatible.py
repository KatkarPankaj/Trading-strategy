"""Minimal OpenAI-compatible chat-completions transport for advisory AI research."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..security import Secret
from .analyst import AIUnavailable

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MAX_RESPONSE_BYTES = 1_048_576


class OpenAICompatibleProvider:
    """Call an explicitly configured OpenAI-compatible HTTPS endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Secret,
        *,
        timeout: float = 20.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
            pass
        elif parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("AI endpoint must use HTTPS (HTTP is allowed only on loopback)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("AI endpoint must not embed credentials, query, or fragment")
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError("AI model must be a non-empty name of at most 128 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 1 <= timeout <= 120:
            raise ValueError("AI timeout must be between 1 and 120 seconds")
        if not isinstance(api_key, Secret):
            raise TypeError("AI API key must be provided as a Secret")

        self.name = "openai_compatible"
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model = model.strip()
        self._api_key = api_key
        self._timeout = float(timeout)

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        request = Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key.reveal()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise AIUnavailable(f"AI provider returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise AIUnavailable("AI provider request failed") from None

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AIUnavailable("AI provider response exceeded the size limit")
        try:
            payload: Any = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AIUnavailable("AI provider returned invalid JSON") from None

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AIUnavailable("AI provider response has no chat message") from None
        if not isinstance(content, str) or not content.strip():
            raise AIUnavailable("AI provider returned an empty chat message")
        return content
