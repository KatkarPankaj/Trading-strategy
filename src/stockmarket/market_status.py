"""Live market session status (NSE API, Finnhub) with config-hours fallback."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from stockmarket.finnhub_client import fetch_market_status
from stockmarket.quotes.nse import fetch_nse_market_status_raw, is_nse_capital_market_open
from stockmarket.quotes.nse_client import is_valid_market_status_payload
from stockmarket.settings import MarketProfile

logger = logging.getLogger("stockmarket.market_status")

MARKET_DISPLAY_LABELS: dict[str, str] = {
    "NSE": "India NSE",
    "US": "US",
}

_CACHE_KEY = "s_market_status_cache"
_CACHE_TTL_SEC = 30.0

_NSE_PROVIDER = "NSE API"
_US_PROVIDER = "Finnhub"


@dataclass(frozen=True)
class MarketSessionStatus:
    market_id: str
    label: str
    is_open: bool
    source: str  # "api" | "fallback"
    detail: str = ""


def _hhmm_to_time(value: str) -> dt_time:
    hour, minute = value.split(":")
    return dt_time(int(hour), int(minute))


def is_regular_session_open(
    profile: MarketProfile,
    *,
    now: datetime | None = None,
) -> bool:
    """Config-based regular session: weekday and market_open <= t < market_close."""
    tz = ZoneInfo(profile.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    if now.weekday() >= 5:
        return False
    t = now.time()
    open_t = _hhmm_to_time(profile.market_open)
    close_t = _hhmm_to_time(profile.market_close)
    return open_t <= t < close_t


def _open_label(is_open: bool) -> str:
    return "Open" if is_open else "Closed"


def _log_api(market_id: str, provider: str, is_open: bool) -> None:
    logger.info(
        "Market status %s: fetched from %s — %s",
        market_id,
        provider,
        _open_label(is_open),
    )


def _log_fallback(market_id: str, provider: str, reason: str, is_open: bool) -> None:
    logger.warning(
        "Market status %s: %s unavailable (%s); using config hours fallback — %s",
        market_id,
        provider,
        reason,
        _open_label(is_open),
    )


def _fallback_status(
    market_id: str,
    label: str,
    profile: MarketProfile,
    provider: str,
    reason: str,
) -> MarketSessionStatus:
    is_open = is_regular_session_open(profile)
    _log_fallback(market_id, provider, reason, is_open)
    return MarketSessionStatus(
        market_id=market_id,
        label=label,
        is_open=is_open,
        source="fallback",
        detail=reason[:120],
    )


def fetch_nse_session_status(
    profile: MarketProfile,
    label: str,
    *,
    nsefetch: Callable[..., Any] | None,
) -> MarketSessionStatus:
    market_id = "NSE"
    try:
        if nsefetch is None:
            raise RuntimeError("nsepython is not installed")
        payload = fetch_nse_market_status_raw(nsefetch)
        if not isinstance(payload, dict) or not is_valid_market_status_payload(payload):
            raise ValueError(
                "invalid or empty NSE marketStatus payload (API blocked or rate limited)"
            )
        is_open = is_nse_capital_market_open(payload)
        _log_api(market_id, _NSE_PROVIDER, is_open)
        return MarketSessionStatus(
            market_id=market_id,
            label=label,
            is_open=is_open,
            source="api",
        )
    except Exception as exc:
        return _fallback_status(market_id, label, profile, _NSE_PROVIDER, str(exc))


def fetch_us_session_status(profile: MarketProfile, label: str) -> MarketSessionStatus:
    market_id = "US"
    try:
        is_open = bool(fetch_market_status("US").get("isOpen"))
        _log_api(market_id, _US_PROVIDER, is_open)
        return MarketSessionStatus(
            market_id=market_id,
            label=label,
            is_open=is_open,
            source="api",
        )
    except Exception as exc:
        return _fallback_status(market_id, label, profile, _US_PROVIDER, str(exc))


def fetch_market_session_status(
    market_id: str,
    profile: MarketProfile,
    label: str,
    *,
    nsefetch: Callable[..., Any] | None = None,
) -> MarketSessionStatus:
    mid = market_id.upper()
    if mid == "NSE":
        return fetch_nse_session_status(profile, label, nsefetch=nsefetch)
    if mid == "US":
        return fetch_us_session_status(profile, label)
    is_open = is_regular_session_open(profile)
    _log_fallback(mid, "unknown market", "no API provider", is_open)
    return MarketSessionStatus(
        market_id=mid,
        label=label,
        is_open=is_open,
        source="fallback",
    )


def _cache_get(session: Any, market_id: str, *, force: bool) -> dict[str, Any] | None:
    if force:
        return None
    cache = session.get(_CACHE_KEY) if hasattr(session, "get") else None
    if not isinstance(cache, dict):
        return None
    entry = cache.get(market_id)
    if not isinstance(entry, dict):
        return None
    ts = float(entry.get("ts", 0.0) or 0.0)
    if time.time() - ts > _CACHE_TTL_SEC:
        return None
    return entry


def _cache_set(session: Any, market_id: str, status: MarketSessionStatus) -> None:
    cache = session.get(_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache[market_id] = {
        "is_open": status.is_open,
        "source": status.source,
        "label": status.label,
        "detail": status.detail,
        "ts": time.time(),
    }
    session[_CACHE_KEY] = cache


def _from_cache_entry(market_id: str, entry: dict[str, Any]) -> MarketSessionStatus:
    return MarketSessionStatus(
        market_id=market_id,
        label=str(entry.get("label", market_id)),
        is_open=bool(entry.get("is_open")),
        source=str(entry.get("source", "api")),
        detail=str(entry.get("detail", "")),
    )


def clear_market_status_cache(session: Any) -> None:
    if hasattr(session, "pop"):
        session.pop(_CACHE_KEY, None)
    elif isinstance(session, dict):
        session.pop(_CACHE_KEY, None)


def resolve_all_market_statuses(
    profiles: Mapping[str, MarketProfile],
    labels: Mapping[str, str],
    *,
    selected_market: str,
    session: Any,
    nsefetch: Callable[..., Any] | None = None,
) -> tuple[MarketSessionStatus, ...]:
    """Resolve status for all markets; force-fetch selected, cache others briefly."""
    selected = str(selected_market).upper()
    out: list[MarketSessionStatus] = []
    for market_id, profile in profiles.items():
        mid = str(market_id).upper()
        label = str(labels.get(mid, labels.get(market_id, mid)))
        force = mid == selected
        cached = _cache_get(session, mid, force=force)
        if cached is not None:
            out.append(_from_cache_entry(mid, cached))
            continue
        status = fetch_market_session_status(
            mid, profile, label, nsefetch=nsefetch
        )
        _cache_set(session, mid, status)
        out.append(status)
    return tuple(out)
