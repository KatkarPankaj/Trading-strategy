"""Retries with exponential backoff and a circuit breaker for flaky external calls."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.2  # fraction of the delay, randomised either way

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay < 0 or self.max_delay < self.base_delay \
                or not 0 <= self.jitter <= 1:
            raise ValueError("invalid retry policy")

    def delay(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return max(0.0, raw * (1 + self.jitter * (2 * rng() - 1)))


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    *,
    retry_on: tuple[type[BaseException], ...] = (
        ConnectionError, TimeoutError),
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Retry only the listed exceptions; the last one is re-raised, never swallowed."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == policy.max_attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            sleep(policy.delay(attempt))
    raise AssertionError("unreachable")


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """Opens after consecutive failures, fails fast while open, then lets one trial call through."""

    def __init__(self, failure_threshold: int = 5, reset_after: timedelta = timedelta(seconds=30),
                 clock: Callable[[], datetime] | None = None) -> None:
        if failure_threshold < 1 or reset_after <= timedelta(0):
            raise ValueError("invalid circuit breaker settings")
        self._threshold = failure_threshold
        self._reset_after = reset_after
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._failures = 0
        self._opened_at: datetime | None = None
        self._lock = RLock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half_open" if self._clock() - self._opened_at >= self._reset_after else "open"

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self.state == "open":
                raise CircuitOpenError("circuit is open; call skipped")
        try:
            result = fn()
        except Exception:
            with self._lock:
                self._failures += 1
                if self._failures >= self._threshold or self._opened_at is not None:
                    self._opened_at = self._clock()
            raise
        with self._lock:
            self._failures = 0
            self._opened_at = None
        return result
