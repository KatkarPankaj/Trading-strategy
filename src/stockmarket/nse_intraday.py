"""NSE-based intraday OHLCV for Indian equities and Nifty (no Finnhub, no yfinance)."""

from __future__ import annotations

from datetime import datetime, timedelta, time
from typing import Any

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

_NSE_SESSION_START = time(9, 15)
_NSE_SESSION_END = time(15, 30)


def symbol_market(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s == "^NSEI" or s == "NSEI":
        return "nse_index"
    if s.endswith(".NS") or s.endswith(".BO"):
        return "nse_equity"
    return "us"


def nse_equity_root(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if "." in s:
        return s.split(".")[0]
    return s


def _interval_to_minutes(interval: str) -> int:
    m = str(interval).strip().lower()
    if m in ("1m", "1min"):
        return 1
    if m in ("5m", "5min"):
        return 5
    if m in ("15m", "15min"):
        return 15
    if m in ("30m", "30min"):
        return 30
    if m in ("60m", "1h", "1hr"):
        return 60
    raise ValueError(
        f"Unsupported NSE intraday interval: {interval!r} (use 1m,5m,15m,30m,60m)"
    )


def _period_lookback_days(period: str) -> int:
    p = str(period).strip().lower()
    if p.endswith("d") and p[:-1].isdigit():
        return max(1, int(p[:-1]))
    if p.endswith("mo") and p[:-2].isdigit():
        return max(1, int(p[:-2]) * 30)
    if p.endswith("y") and p[:-1].isdigit():
        return max(1, int(p[:-1]) * 365)
    if p.isdigit():
        return max(1, int(p))
    return 5


def _session_timestamps_for_day(day: datetime.date, step_minutes: int) -> list[datetime]:
    start = IST.localize(datetime.combine(day, _NSE_SESSION_START))
    end = IST.localize(datetime.combine(day, _NSE_SESSION_END))
    out: list[datetime] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(minutes=step_minutes)
    return out


def expand_daily_to_ist_intraday(
    daily: pd.DataFrame,
    *,
    interval_minutes: int,
) -> pd.DataFrame:
    """Expand one row per calendar day into IST regular-session bars (synthetic path)."""
    if daily.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = daily.copy()
    df["date_only"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date_only"])
    rows: list[dict[str, Any]] = []
    idx: list[datetime] = []
    for _, r in df.iterrows():
        day = r["date_only"]
        o = float(r["open"])
        h = float(r["high"])
        l = float(r["low"])
        c = float(r["close"])
        vol = float(r.get("volume", 0.0) or 0.0)
        stamps = _session_timestamps_for_day(day, interval_minutes)
        n = max(1, len(stamps))
        v_each = vol / float(n) if vol > 0 else 0.0
        for i, ts in enumerate(stamps):
            t = i / max(1, n - 1)
            price = o + (c - o) * t
            bar_h = max(price, h * 0.5 + price * 0.5)
            bar_l = min(price, l * 0.5 + price * 0.5)
            rows.append(
                {
                    "open": price,
                    "high": max(bar_h, price),
                    "low": min(bar_l, price),
                    "close": price,
                    "volume": v_each,
                }
            )
            idx.append(ts)
    out = pd.DataFrame(rows)
    out.index = pd.DatetimeIndex(idx, tz="Asia/Kolkata")
    out.index = out.index.tz_convert("UTC")
    return out[["open", "high", "low", "close", "volume"]]


def _fetch_daily_equity_via_bhavcopy(symbol: str, max_days: int) -> pd.DataFrame:
    try:
        from nsepython import get_bhavcopy
    except Exception as exc:
        raise ValueError(
            "nsepython is required for NSE equity intraday. Run: pip install nsepython"
        ) from exc

    root = nse_equity_root(symbol)
    rows: list[dict[str, Any]] = []
    d = datetime.now().date()
    attempts = 0
    while len(rows) < max_days and attempts < max_days + 40:
        attempts += 1
        if d.weekday() >= 5:
            d -= timedelta(days=1)
            continue
        ds = d.strftime("%d-%m-%Y")
        try:
            bh = get_bhavcopy(ds)
        except Exception:
            d -= timedelta(days=1)
            continue
        if bh is None or bh.empty or "SYMBOL" not in bh.columns:
            d -= timedelta(days=1)
            continue
        hit = bh[bh["SYMBOL"].astype(str).str.strip() == root]
        if hit.empty:
            d -= timedelta(days=1)
            continue
        r0 = hit.iloc[0]
        try:
            kv = {str(k).strip(): r0[k] for k in r0.index}
            raw_date = str(kv.get("DATE1", "")).strip()
            dt = pd.to_datetime(raw_date, errors="coerce")
            if pd.isna(dt):
                dt = pd.Timestamp(d)
            o = float(kv.get("OPEN_PRICE", 0) or 0)
            h = float(kv.get("HIGH_PRICE", 0) or 0)
            low = float(kv.get("LOW_PRICE", 0) or 0)
            c = float(kv.get("CLOSE_PRICE", 0) or 0)
            v = float(str(kv.get("TTL_TRD_QNTY", "0")).replace(",", "").strip() or 0)
        except Exception:
            d -= timedelta(days=1)
            continue
        rows.append(
            {
                "date": dt.normalize(),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
            }
        )
        d -= timedelta(days=1)

    if not rows:
        raise ValueError(
            f"No NSE bhavcopy rows collected for {symbol!r} — check symbol and trading calendar."
        )
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return out


def _fetch_daily_nifty50_via_index_history(max_days: int) -> pd.DataFrame:
    try:
        from nsepython import index_history
    except Exception as exc:
        raise ValueError("nsepython index_history unavailable") from exc

    end = datetime.now()
    start = end - timedelta(days=max_days + 10)
    df = index_history(
        "Nifty 50",
        start.strftime("%d-%b-%Y"),
        end.strftime("%d-%b-%Y"),
    )
    if df is None or df.empty:
        raise ValueError("index_history returned empty for Nifty 50")
    if "HistoricalDate" not in df.columns:
        raise ValueError(f"Unexpected index_history columns: {df.columns.tolist()}")
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["HistoricalDate"], errors="coerce"),
            "open": pd.to_numeric(df["OPEN"], errors="coerce"),
            "high": pd.to_numeric(df["HIGH"], errors="coerce"),
            "low": pd.to_numeric(df["LOW"], errors="coerce"),
            "close": pd.to_numeric(df["CLOSE"], errors="coerce"),
            "volume": 0.0,
        }
    ).dropna(subset=["date"])
    out = out.sort_values("date").tail(max_days).reset_index(drop=True)
    return out


def fetch_daily_equity_series_bhavcopy(symbol: str, max_days: int) -> pd.DataFrame:
    """Public wrapper: NSE equity daily OHLCV from bhavcopy archives."""
    return _fetch_daily_equity_via_bhavcopy(symbol, max_days)


def fetch_nse_intraday_ohlcv(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Daily-backed NSE India series expanded to IST session intraday bars."""
    mkt = symbol_market(symbol)
    if mkt == "us":
        raise ValueError("fetch_nse_intraday_ohlcv expects an NSE symbol")
    days = max(1, _period_lookback_days(period))
    step = _interval_to_minutes(interval)
    if mkt == "nse_index":
        daily = _fetch_daily_nifty50_via_index_history(days + 5)
    else:
        daily = _fetch_daily_equity_via_bhavcopy(symbol, days + 5)
    daily = daily.tail(days + 2)
    return expand_daily_to_ist_intraday(daily, interval_minutes=step)
