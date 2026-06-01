"""NSE quote-equity JSON parsing and raw fetch."""

from __future__ import annotations

from typing import Any, Callable

from .types import Quote


def to_nse_symbol(symbol: str) -> str:
    return str(symbol).split(".")[0].upper()


def quote_from_price_info(
    symbol: str,
    payload: dict[str, Any],
    *,
    provider: str = "nse",
) -> Quote:
    """Build Quote from NSE `priceInfo` block (and optional root for open)."""
    p = payload.get("priceInfo", {}) or {}
    if not p and isinstance(payload, dict):
        p = payload

    price = float(p.get("lastPrice") or 0.0)
    vwap = float(p.get("vwap") or 0.0)
    pchange = float(p.get("pChange") or 0.0)
    open_price = float(p.get("open") or payload.get("open") or 0.0)
    ihl = p.get("intraDayHighLow", {}) or {}
    day_low = float(ihl.get("min") or 0.0)
    day_high = float(ihl.get("max") or 0.0)
    range_pct = (
        ((day_high - day_low) / max(price, 1e-6)) * 100 if price > 0 else 0.0
    )
    return Quote(
        symbol=symbol,
        price=price,
        vwap=vwap,
        pchange=pchange,
        day_high=day_high,
        day_low=day_low,
        open_price=open_price,
        range_pct=range_pct,
        provider=provider,
    )


def fetch_nse_quote_equity_raw(
    nse_symbol: str,
    nsefetch: Callable[..., Any],
) -> dict[str, Any]:
    """Fetch full quote-equity JSON (wraps nsefetch URL or symbol helper)."""
    return nsefetch(
        f"https://www.nseindia.com/api/quote-equity?symbol={nse_symbol}"
    )


_NSE_MARKET_STATUS_URL = "https://www.nseindia.com/api/marketStatus"
_CAPITAL_MARKET_SEGMENT = "capital market"


def fetch_nse_market_status_raw(nsefetch: Callable[..., Any]) -> dict[str, Any]:
    """Fetch NSE marketStatus JSON (all segments)."""
    return nsefetch(_NSE_MARKET_STATUS_URL)


def is_nse_capital_market_open(payload: dict[str, Any]) -> bool:
    """True when Capital Market segment reports marketStatus Open."""
    segments = payload.get("marketState") or payload.get("marketstate") or []
    if not isinstance(segments, list):
        return False
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        name = str(seg.get("market") or seg.get("marketType") or "").strip().lower()
        if name != _CAPITAL_MARKET_SEGMENT:
            continue
        status = str(seg.get("marketStatus") or seg.get("marketstatus") or "").strip().lower()
        return status == "open"
    return False
