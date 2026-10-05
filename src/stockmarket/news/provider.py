"""Provider-independent contract for retrieving normalized news events."""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import NewsEvent, NewsQuery


@runtime_checkable
class NewsProvider(Protocol):
    """Read-only news source; implementations may not execute or place orders."""

    def get_news(self, query: NewsQuery) -> Sequence[NewsEvent]:
        """Return events matching the query, ordered newest first when possible."""
        ...
