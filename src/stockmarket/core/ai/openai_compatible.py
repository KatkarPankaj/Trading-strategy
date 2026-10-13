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
        max_output_tokens: int | None = None,
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
        if max_output_tokens is not None and (
                isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
                or not 1 <= max_output_tokens <= 32_768):
            raise ValueError("AI max output tokens must be from 1 to 32768")

        self.name = "openai_compatible"
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model = model.strip()
        self._api_key = api_key
        self._timeout = float(timeout)
        self._max_output_tokens = max_output_tokens
        self._structured_outputs = parsed.hostname == "api.openai.com"

    def complete(self, system: str, user: str) -> str:
        return self.complete_with_usage(system, user)[0]

    def complete_with_usage(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, int] | None]:
        """Return the chat message and provider-reported token usage when present."""
        return self._request(system, user, {"type": "json_object"})

    def complete_structured_with_usage(
        self, system: str, user: str, name: str, schema: dict[str, Any],
    ) -> tuple[str, dict[str, int] | None]:
        if not self._structured_outputs:
            return self.complete_with_usage(system, user)
        return self._request(system, user, {
            "type": "json_schema",
            "json_schema": {
                "name": name, "strict": True, "schema": _strict_schema(schema),
            },
        })

    def _request(
        self, system: str, user: str, response_format: dict[str, Any],
    ) -> tuple[str, dict[str, int] | None]:
        options: dict[str, Any] = {}
        if self._max_output_tokens is not None:
            # Chat Completions "max_tokens"; some reasoning models require
            # "max_completion_tokens" instead and may reject this field.
            options["max_tokens"] = self._max_output_tokens
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": response_format,
            **options,
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
            raise AIUnavailable(
                f"AI provider returned HTTP {exc.code}",
                retryable=exc.code in {408, 429} or exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise AIUnavailable("AI provider request failed", retryable=True) from None

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
        usage = payload.get("usage") if isinstance(payload, dict) else None
        reported = None
        if isinstance(usage, dict):
            reported = {
                key: value for key in ("prompt_tokens", "completion_tokens")
                if isinstance(value := usage.get(key), int)
                and not isinstance(value, bool) and value >= 0
            } or None
        return content, reported


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Require every object field for OpenAI strict mode; nullable fields stay nullable."""
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "default":
            continue
        if isinstance(value, dict):
            result[key] = _strict_schema(value)
        elif isinstance(value, list):
            result[key] = [
                _strict_schema(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            result[key] = value
    if result.get("type") == "object":
        result["required"] = list(result.get("properties", {}))
        result["additionalProperties"] = False
    return result
