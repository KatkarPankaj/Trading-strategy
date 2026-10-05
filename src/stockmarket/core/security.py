"""Security helpers: secret handling, config scanning, URL and input validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

SECRET_KEY_MARKERS = ("api_key", "apikey", "secret", "password", "passwd", "token",
                      "authorization", "credential", "private_key", "access_key")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # user:password@host in a URL
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)?[-_]?[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SYMBOL = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.\-_=&:^]{0,31}$")


class SecurityError(ValueError):
    pass


class SecretMissingError(SecurityError):
    pass


class ConfigSecretError(SecurityError):
    """Credentials were found where only non-secret configuration is allowed."""


class Secret:
    """Holds a credential; str/repr never reveal it, only reveal() does."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecurityError("a secret must be a non-empty string")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Secret is immutable")

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("Secret cannot be pickled")


def get_secret(name: str, env: Mapping[str, str] | None = None, *, required: bool = True) -> Secret | None:
    """Read NAME from the environment, or from the file named by NAME_FILE (Docker/Kubernetes secrets)."""
    env = os.environ if env is None else env
    value = env.get(name)
    file_path = env.get(f"{name}_FILE")
    if not value and file_path:
        path = Path(file_path)
        if not path.is_file():
            raise SecretMissingError(f"{name}_FILE does not point to a file")
        value = path.read_text(encoding="utf-8").strip()
    if not value:
        if required:
            raise SecretMissingError(f"required secret {name} is not set")
        return None
    return Secret(value)


def assert_no_secrets_in_config(config: Mapping[str, Any], source: str = "config") -> None:
    """Fail if a config mapping holds credential-like keys or values; secrets belong in the environment."""
    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, inner in value.items():
                if any(m in str(key).lower() for m in SECRET_KEY_MARKERS):
                    raise ConfigSecretError(
                        f"{source}: secret-like key '{path}{key}' is not allowed")
                walk(inner, f"{path}{key}.")
        elif isinstance(value, (list, tuple)):
            for n, item in enumerate(value):
                walk(item, f"{path}{n}.")
        elif isinstance(value, str):
            if any(p.search(value) for p in _SECRET_VALUE_PATTERNS):
                raise ConfigSecretError(
                    f"{source}: value at '{path.rstrip('.')}' looks like a credential")
    walk(config, "")


def redact_dsn(url: str) -> str:
    """Mask the password in a database URL for logging."""
    parsed = urlparse(url)
    if parsed.password is None:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    netloc = f"{parsed.username or ''}:***@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


def require_https(url: str, *, allowed_hosts: frozenset[str] | None = None) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SecurityError("external services must be reached over https")
    if parsed.username or parsed.password:
        raise SecurityError("credentials must not be embedded in URLs")
    if allowed_hosts is not None and parsed.hostname.lower() not in allowed_hosts:
        raise SecurityError(
            f"host {parsed.hostname!r} is not in the allowed list")
    return url


def validate_symbol(symbol: str) -> str:
    """Accept ticker-style symbols only; blocks path, shell and query metacharacters."""
    if not isinstance(symbol, str) or not _SYMBOL.match(symbol):
        raise SecurityError(f"invalid symbol: {symbol!r}")
    return symbol


def safe_join(base: Path, *parts: str) -> Path:
    """Join untrusted path parts under `base`, refusing any result that escapes it."""
    root = base.resolve()
    target = root.joinpath(*parts).resolve()
    if root != target and root not in target.parents:
        raise SecurityError("path escapes the permitted directory")
    return target
