"""Finnhub REST client for US equities (quotes + OHLC candles)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

_DOTENV_LOADED = False


def _load_dotenv_files() -> None:
    """Load `.env` from repo root, then cwd (cwd wins on duplicate keys). Idempotent."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env")
    load_dotenv(Path.cwd() / ".env", override=True)


_load_dotenv_files()


def finnhub_api_key() -> str:
    key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    if not key:
        raise ValueError(
            "FINNHUB_API_KEY is not set. Add it to a gitignored `.env` in the repo root "
            "(see `.env.example`), or export it in your environment."
        )
    return key


def fetch_quote(symbol: str, *, timeout: float = 10.0, max_retries: int = 3) -> dict[str, Any]:
    """GET /quote — returns Finnhub JSON (expects keys c, h, l, o, pc)."""
    if requests is None:
        raise RuntimeError("requests is required for Finnhub. Run: pip install requests")
    sym = str(symbol).upper().strip()
    token = finnhub_api_key()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.get(
                f"{FINNHUB_BASE_URL}/quote",
                params={"symbol": sym, "token": token},
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "c" in data:
                return data
            last_exc = ValueError(f"Unexpected Finnhub quote payload for {sym}")
        except Exception as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            time.sleep(0.35 * (2**attempt))
    raise ValueError(f"Finnhub quote failed for {sym}: {last_exc}")


def _interval_to_resolution(interval: str) -> str:
    m = str(interval).strip().lower()
    if m in ("1m", "1min"):
        return "1"
    if m in ("5m", "5min"):
        return "5"
    if m in ("15m", "15min"):
        return "15"
    if m in ("30m", "30min"):
        return "30"
    if m in ("60m", "1h", "1hr"):
        return "60"
    if m in ("1d", "d", "day"):
        return "D"
    raise ValueError(f"Unsupported Finnhub interval: {interval!r}")


def _period_to_seconds(period: str) -> int:
    p = str(period).strip().lower()
    if p.endswith("d") and p[:-1].isdigit():
        return int(p[:-1]) * 86400
    if p.endswith("mo") and p[:-2].isdigit():
        return int(p[:-2]) * 30 * 86400
    if p.endswith("y") and p[:-1].isdigit():
        return int(p[:-1]) * 365 * 86400
    if p.isdigit():
        return int(p) * 86400
    raise ValueError(f"Unsupported period string: {period!r}")


def fetch_candles(
    symbol: str,
    *,
    resolution: str,
    from_unix: int,
    to_unix: int,
    timeout: float = 15.0,
    max_retries: int = 4,
) -> dict[str, Any]:
    """GET /stock/candle — returns Finnhub candle JSON (t, o, h, l, c, v, s)."""
    if requests is None:
        raise RuntimeError("requests is required for Finnhub.")
    sym = str(symbol).upper().strip()
    token = finnhub_api_key()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.get(
                f"{FINNHUB_BASE_URL}/stock/candle",
                params={
                    "symbol": sym,
                    "resolution": resolution,
                    "from": int(from_unix),
                    "to": int(to_unix),
                    "token": token,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data
            last_exc = ValueError("Non-dict Finnhub candle response")
        except Exception as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            time.sleep(0.5 * (2**attempt))
    raise ValueError(f"Finnhub candles failed for {sym}: {last_exc}")


def fetch_intraday_candles_df_params(
    symbol: str,
    interval: str,
    period: str,
) -> tuple[str, int, int]:
    """Map yfinance-style interval/period to Finnhub resolution and unix window."""
    res = _interval_to_resolution(interval)
    span = max(86400, _period_to_seconds(period))
    to_u = int(time.time())
    from_u = to_u - span
    return res, from_u, to_u
