"""Structured JSON logging with secret redaction and shared error counters."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
REDACTED = "***REDACTED***"
_SECRET_KEYS = ("api_key", "apikey", "secret", "password", "passwd", "token",
                "authorization", "credential", "private_key", "dsn")
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[=:]\s*)(\S+)")

LogSink = Callable[[dict[str, Any]], None]


def redact(value: Any) -> Any:
    """Mask secret-looking keys and key=value fragments in strings, recursively."""
    if isinstance(value, Mapping):
        return {k: REDACTED if any(m in str(k).lower() for m in _SECRET_KEYS) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
    return value


class ErrorCounter:
    """Counts ERROR and CRITICAL events per component; shared with the health monitor."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = RLock()

    def increment(self, component: str) -> None:
        with self._lock:
            self._counts[component] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)


def json_logging_sink(logger_name: str = "stockmarket") -> LogSink:
    """Emit one JSON line per event through the standard logging module."""
    target = logging.getLogger(logger_name)

    def sink(record: dict[str, Any]) -> None:
        target.log(_LEVELS[record["severity"]], json.dumps(
            record, sort_keys=True, default=_json_default))
    return sink


def persistence_sink(system_events: Any) -> LogSink:
    """Store events (WARNING and above) through a SystemEventRepository."""
    def sink(record: dict[str, Any]) -> None:
        if _LEVELS[record["severity"]] < logging.WARNING:
            return
        extra = {k: v for k, v in record.items()
                 if k not in ("timestamp", "component", "severity", "message", "correlation_id")}
        system_events.record(record["component"], record["severity"], record["message"],
                             timestamp=datetime.fromisoformat(
                                 record["timestamp"]),
                             correlation_id=record["correlation_id"],
                             payload=json.loads(json.dumps(extra, default=_json_default)))
    return sink


class StructuredLogger:
    """Every event carries timestamp, component, symbol, strategy, correlation_id, signal_id, order_id, severity, message."""

    def __init__(
        self,
        component: str,
        sinks: list[LogSink] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        errors: ErrorCounter | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if not component.strip():
            raise ValueError("component must be non-empty")
        self.component = component
        self._sinks = sinks if sinks is not None else [json_logging_sink()]
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.errors = errors or ErrorCounter()
        self._context = dict(context or {})

    def bind(self, **context: Any) -> "StructuredLogger":
        """Child logger sharing sinks and counters, with default fields (e.g. correlation_id)."""
        child = StructuredLogger(self.component, self._sinks, clock=self._clock,
                                 errors=self.errors, context={**self._context, **context})
        return child

    def log(self, severity: str, message: str, *, symbol: str | None = None,
            strategy: str | None = None, correlation_id: str | None = None,
            signal_id: Any = None, order_id: Any = None, **extra: Any) -> dict[str, Any]:
        severity = severity.upper()
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        ctx = self._context
        record = redact({
            "timestamp": self._clock().astimezone(timezone.utc).isoformat(),
            "component": self.component,
            "symbol": symbol or ctx.get("symbol"),
            "strategy": strategy or ctx.get("strategy"),
            "correlation_id": correlation_id or ctx.get("correlation_id") or uuid4().hex,
            "signal_id": _text(signal_id if signal_id is not None else ctx.get("signal_id")),
            "order_id": _text(order_id if order_id is not None else ctx.get("order_id")),
            "severity": severity,
            "message": message,
            **{**{k: v for k, v in ctx.items() if k not in
                  ("symbol", "strategy", "correlation_id", "signal_id", "order_id")}, **extra},
        })
        if _LEVELS[severity] >= logging.ERROR:
            self.errors.increment(self.component)
        for sink in self._sinks:
            try:
                sink(record)
            except Exception:  # a broken sink must not take down trading, but must be visible
                logging.getLogger("stockmarket.observability").exception(
                    "log sink failed")
        return record

    def debug(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("DEBUG", message, **kw)

    def info(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("INFO", message, **kw)

    def warning(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("WARNING", message, **kw)

    def error(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("ERROR", message, **kw)

    def critical(self, message: str, **kw: Any) -> dict[str, Any]:
        return self.log("CRITICAL", message, **kw)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
