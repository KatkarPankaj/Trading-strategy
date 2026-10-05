"""Trading gate: independent reasons that block new entries while exits stay possible."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Callable


class TradingGate:
    """Open by default. Each reason (reconciliation, kill switch, ...) must be cleared by its owner."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._reasons: dict[str, tuple[str, datetime]] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def halt(self, code: str, detail: str = "") -> None:
        with self._lock:
            self._reasons.setdefault(code, (detail, self._clock()))

    def clear(self, code: str) -> None:
        with self._lock:
            self._reasons.pop(code, None)

    def blocked_reason(self) -> str | None:
        with self._lock:
            if not self._reasons:
                return None
            return "; ".join(f"{c}: {d}" if d else c for c, (d, _) in sorted(self._reasons.items()))

    @property
    def halted(self) -> bool:
        return self.blocked_reason() is not None

    def reasons(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {c: {"detail": d, "since": t.isoformat()} for c, (d, t) in self._reasons.items()}
