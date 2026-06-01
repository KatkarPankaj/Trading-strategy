"""Hardened NSE India HTTP session (cookie warmup + API headers).

nsepython's bundled ``nsefetch`` warms cookies with document-style headers and
swallows non-JSON responses as ``{}``. This module uses a shared session, proper
API headers, and raises explicit errors so callers can fall back or surface failures.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import requests

from .nse import to_nse_symbol

logger = logging.getLogger("stockmarket.quotes.nse_client")

_WARMUP_URL = "https://www.nseindia.com/market-data/live-equity-market"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_PAGE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}
_API_HEADERS_BASE = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_session_lock = threading.Lock()
_shared_session: requests.Session | None = None


class NseFetchError(Exception):
    """NSE HTTP/API failure (blocked, empty body, or invalid JSON)."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        url: str = "",
        body_preview: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self.url = url
        self.body_preview = body_preview
        super().__init__(message)

    def user_message(self) -> str:
        if self.status_code == 403:
            return (
                "NSE blocked the quote request (HTTP 403). "
                "Try again from a residential network or when the NSE feed is available."
            )
        if self.status_code == 200 and "empty" in str(self).lower():
            return (
                "NSE returned an empty response (feed blocked or rate limited). "
                "Signals cannot be computed until quotes load."
            )
        return f"NSE quote fetch failed: {self}"


def is_valid_quote_equity_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    pi = payload.get("priceInfo")
    if not isinstance(pi, dict):
        return False
    try:
        return float(pi.get("lastPrice") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def is_valid_market_status_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    segments = payload.get("marketState") or payload.get("marketstate")
    return isinstance(segments, list) and len(segments) > 0


def _get_session() -> requests.Session:
    global _shared_session
    with _session_lock:
        if _shared_session is None:
            _shared_session = requests.Session()
        return _shared_session


def _warm_session(session: requests.Session) -> None:
    r = session.get(_WARMUP_URL, headers=_PAGE_HEADERS, timeout=15)
    if r.status_code >= 400:
        logger.warning(
            "NSE cookie warmup returned HTTP %s for %s",
            r.status_code,
            _WARMUP_URL,
        )


def _api_headers(referer: str) -> dict[str, str]:
    h = dict(_API_HEADERS_BASE)
    h["Referer"] = referer
    return h


class HardenedNseClient:
    """Thread-safe NSE JSON fetcher with session reuse."""

    def __init__(self) -> None:
        self._warm_lock = threading.Lock()
        self._warmed = False

    def _ensure_warm(self, session: requests.Session) -> None:
        with self._warm_lock:
            if not self._warmed:
                _warm_session(session)
                self._warmed = True

    def fetch_json(self, url: str, *, referer: str | None = None) -> dict[str, Any]:
        session = _get_session()
        self._ensure_warm(session)
        ref = referer or "https://www.nseindia.com/"
        r = session.get(url, headers=_api_headers(ref), timeout=15)
        if r.status_code != 200:
            raise NseFetchError(
                r.status_code,
                f"HTTP {r.status_code} from NSE",
                url=url,
                body_preview=(r.text or "")[:240],
            )
        try:
            data = r.json()
        except ValueError as exc:
            raise NseFetchError(
                r.status_code,
                "non-JSON response from NSE",
                url=url,
                body_preview=(r.text or "")[:240],
            ) from exc
        if not isinstance(data, dict):
            raise NseFetchError(
                r.status_code,
                f"unexpected JSON type {type(data).__name__}",
                url=url,
            )
        if not data:
            raise NseFetchError(
                200,
                "empty JSON object from NSE (blocked or rate limited)",
                url=url,
            )
        return data

    def fetch_quote_equity(self, nse_symbol: str) -> dict[str, Any]:
        sym = to_nse_symbol(nse_symbol)
        url = f"https://www.nseindia.com/api/quote-equity?symbol={sym}"
        referer = f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"
        data = self.fetch_json(url, referer=referer)
        if not is_valid_quote_equity_payload(data):
            raise NseFetchError(
                200,
                f"quote-equity payload missing priceInfo.lastPrice for {sym}",
                url=url,
            )
        return data

    def fetch_market_status(self) -> dict[str, Any]:
        url = "https://www.nseindia.com/api/marketStatus"
        data = self.fetch_json(url, referer="https://www.nseindia.com/")
        if not is_valid_market_status_payload(data):
            raise NseFetchError(
                200,
                "marketStatus payload missing marketState segments",
                url=url,
            )
        return data


_default_client: HardenedNseClient | None = None


def get_hardened_nse_client() -> HardenedNseClient:
    global _default_client
    if _default_client is None:
        _default_client = HardenedNseClient()
    return _default_client


def hardened_nsefetch(url: str) -> dict[str, Any]:
    """Drop-in replacement for ``nsepython.nsefetch`` with explicit failures."""
    client = get_hardened_nse_client()
    url = str(url).strip()
    if "marketStatus" in url:
        return client.fetch_market_status()
    if "quote-equity" in url:
        if "symbol=" in url:
            sym = url.split("symbol=", 1)[-1].split("&", 1)[0]
            return client.fetch_quote_equity(sym)
    return client.fetch_json(
        url,
        referer="https://www.nseindia.com/market-data/live-equity-market",
    )


def create_hardened_nse_fetch() -> Callable[..., Any]:
    return hardened_nsefetch


def fetch_nse_quote_equity_hardened(nse_symbol: str) -> dict[str, Any]:
    return get_hardened_nse_client().fetch_quote_equity(nse_symbol)


def fetch_nse_market_status_hardened() -> dict[str, Any]:
    return get_hardened_nse_client().fetch_market_status()


# Re-export for tests that patch fetch helpers
__all__ = [
    "NseFetchError",
    "HardenedNseClient",
    "create_hardened_nse_fetch",
    "hardened_nsefetch",
    "is_valid_quote_equity_payload",
    "is_valid_market_status_payload",
]
