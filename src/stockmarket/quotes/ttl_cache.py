"""Process-local TTL cache for quote fetches (Streamlit-agnostic)."""

from __future__ import annotations

import time
from typing import Any, Callable, Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TtlCache(Generic[K, V]):
    def __init__(self, default_ttl_sec: float) -> None:
        self.default_ttl_sec = default_ttl_sec
        self._store: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        item = self._store.get(key)
        if item is None:
            return None
        exp, val = item
        if time.monotonic() > exp:
            del self._store[key]
            return None
        return val

    def set(self, key: K, value: V, ttl_sec: float | None = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
        self._store[key] = (time.monotonic() + ttl, value)

    def get_or_set(
        self,
        key: K,
        factory: Callable[[], V],
        ttl_sec: float | None = None,
    ) -> V:
        v = self.get(key)
        if v is not None:
            return v
        v = factory()
        self.set(key, v, ttl_sec=ttl_sec)
        return v

    def clear(self) -> None:
        self._store.clear()
