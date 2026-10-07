"""Read-only company-news adapter for Finnhub's timestamped REST API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid5

from .models import MarketImpact, NewsEvent, NewsEventType, NewsQuery, NewsSentiment

_ENDPOINT = "https://finnhub.io/api/v1/company-news"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_EVENT_NAMESPACE = UUID("42e5f88b-2026-49d9-b1ca-5d8b48bbf3d1")


class FinnhubNewsUnavailable(RuntimeError):
    """Finnhub is unavailable or returned data outside the supported contract."""


class FinnhubNewsProvider:
    """Retrieve bounded, timestamped company-news events without placing orders."""

    name = "finnhub"

    def __init__(
        self,
        api_key: str,
        *,
        symbol_map: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Finnhub API key must be a non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be greater than 0 and at most 60 seconds")
        self._api_key = api_key.strip()
        self._timeout = float(timeout)
        self._symbol_map: dict[str, str] = {}
        for key, value in (symbol_map or {}).items():
            if not isinstance(key, str) or ":" not in key \
                    or not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "Finnhub symbol mappings must map MARKET:SYMBOL to a provider symbol")
            normalized = ":".join(part.strip().upper() for part in key.split(":", 1))
            if normalized in self._symbol_map:
                raise ValueError(f"duplicate Finnhub mapping {normalized!r}")
            self._symbol_map[normalized] = value.strip().upper()

    def _provider_symbol(self, market: str, symbol: str) -> str:
        key = f"{market.upper()}:{symbol.upper()}"
        if key in self._symbol_map:
            return self._symbol_map[key]
        if market.upper() == "US":
            return symbol.upper()
        raise FinnhubNewsUnavailable(
            f"explicit Finnhub symbol mapping is required for {key}")

    def get_news(self, query: NewsQuery) -> tuple[NewsEvent, ...]:
        if len(query.symbols) != 1:
            raise ValueError("Finnhub company news requires exactly one symbol")
        if query.affected_market is None:
            raise ValueError("Finnhub company news requires an affected_market")
        if query.start_time is None or query.end_time is None:
            raise ValueError("Finnhub company news requires start_time and end_time")
        if query.limit > 100:
            raise ValueError("Finnhub company news limit must not exceed 100")

        symbol = query.symbols[0]
        provider_symbol = self._provider_symbol(query.affected_market, symbol)
        start_date = query.start_time.astimezone(timezone.utc).date()
        end_date = query.end_time.astimezone(timezone.utc).date()
        params = urlencode({
            "symbol": provider_symbol,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        })
        request = Request(
            f"{_ENDPOINT}?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": "Trading-strategy-research/1.0",
                "X-Finnhub-Token": self._api_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise FinnhubNewsUnavailable(
                    "Finnhub response exceeded the size limit")
            payload = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            reason = "rate limit exceeded" if exc.code == 429 else (
                "authentication or entitlement rejected" if exc.code in (401, 403)
                else f"HTTP {exc.code}")
            raise FinnhubNewsUnavailable(
                f"Finnhub company-news request failed: {reason}") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError,
                json.JSONDecodeError) as exc:
            raise FinnhubNewsUnavailable(
                f"Finnhub company-news request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, list):
            raise FinnhubNewsUnavailable(
                "Finnhub company-news response must be a list")
        if len(payload) > 1000:
            raise FinnhubNewsUnavailable(
                "Finnhub returned more than 1000 events for one query")

        start = query.start_time.astimezone(timezone.utc)
        end = query.end_time.astimezone(timezone.utc)
        events: list[NewsEvent] = []
        seen_ids: set[str] = set()
        for row in payload:
            if not isinstance(row, dict):
                raise FinnhubNewsUnavailable(
                    "Finnhub company-news response contains an invalid event")
            raw_id = row.get("id")
            timestamp = row.get("datetime")
            headline = row.get("headline")
            source = row.get("source")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) \
                    or not str(raw_id).strip() or isinstance(timestamp, bool) \
                    or not isinstance(timestamp, (int, float)) \
                    or not isfinite(timestamp) \
                    or not isinstance(headline, str) or not headline.strip() \
                    or not isinstance(source, str) or not source.strip():
                raise FinnhubNewsUnavailable(
                    "Finnhub event is missing a valid id, timestamp, headline or source")
            event_id = str(raw_id)
            if event_id in seen_ids:
                raise FinnhubNewsUnavailable(
                    "Finnhub returned duplicate event identifiers")
            seen_ids.add(event_id)
            try:
                observed_at = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError) as exc:
                raise FinnhubNewsUnavailable(
                    "Finnhub event has an invalid timestamp") from exc
            if observed_at < start or observed_at > end:
                continue
            summary = row.get("summary")
            if summary is not None and not isinstance(summary, str):
                raise FinnhubNewsUnavailable(
                    "Finnhub event summary has an invalid type")
            reference = row.get("url")
            if reference is not None and (
                    not isinstance(reference, str)
                    or not reference.startswith("https://")):
                raise FinnhubNewsUnavailable(
                    "Finnhub event reference must be an HTTPS URL")
            related = row.get("related")
            if related is not None and not isinstance(related, str):
                raise FinnhubNewsUnavailable(
                    "Finnhub event related-symbol field has an invalid type")
            if related and provider_symbol.upper() not in {
                    value.strip().upper() for value in related.split(",")}:
                continue

            events.append(NewsEvent(
                timestamp=observed_at,
                source=f"{self.name}:{source.strip()}",
                headline=headline.strip()[:1000],
                event_type=NewsEventType.UNKNOWN,
                sentiment=NewsSentiment.UNKNOWN,
                sentiment_confidence=0.0,
                market_impact=MarketImpact.UNKNOWN,
                relevance=1.0,
                symbol=symbol,
                content=summary.strip()[:4000] if summary and summary.strip() else None,
                reference=reference,
                affected_market=query.affected_market,
                provider_event_id=event_id,
                event_id=uuid5(
                    _EVENT_NAMESPACE,
                    sha256(f"{self.name}:{event_id}".encode()).hexdigest(),
                ),
            ))
        events.sort(key=lambda item: item.timestamp, reverse=True)
        return tuple(events[:query.limit])
