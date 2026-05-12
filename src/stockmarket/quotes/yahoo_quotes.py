"""Yahoo Finance multi-symbol intraday quote helpers."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import yfinance_tz  # noqa: F401  # configure cache path before yfinance

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore

from .types import Quote


def _intraday_bar_to_quote(
    symbol: str,
    intraday: pd.DataFrame,
    pchange_override: float | None = None,
) -> Quote | None:
    if intraday is None or intraday.empty:
        return None
    intraday = intraday.dropna(subset=["Close"]).copy()
    if intraday.empty:
        return None
    _flatten_cols(intraday)
    intraday.columns = [str(c).strip() for c in intraday.columns]

    close_col = "Close" if "Close" in intraday.columns else "close"
    high_col = "High" if "High" in intraday.columns else "high"
    low_col = "Low" if "Low" in intraday.columns else "low"
    open_col = "Open" if "Open" in intraday.columns else "open"

    latest = intraday.iloc[-1]
    price = float(latest.get(close_col) or 0.0)
    high = float(intraday[high_col].max() or price)
    low = float(intraday[low_col].min() or price)
    open_price = float(intraday.iloc[0].get(open_col) or price)

    vol_col = "Volume" if "Volume" in intraday.columns else "volume"
    if vol_col in intraday.columns:
        vs = intraday[vol_col]
        if float(vs.fillna(0).sum()) > 0:
            c = intraday[close_col].fillna(0.0)
            vwap = float((c * vs.fillna(0.0)).sum() / max(float(vs.fillna(0.0).sum()), 1e-6))
        else:
            vwap = (high + low + open_price + price) / 4.0 if price > 0 else 0.0
    else:
        vwap = (high + low + open_price + price) / 4.0 if price > 0 else 0.0

    range_pct = ((high - low) / max(price, 1e-6)) * 100.0 if price > 0 else 0.0
    pchange = (
        float(pchange_override)
        if pchange_override is not None
        else _pchange_from_ticker(symbol, price)
    )
    return Quote(
        symbol=symbol,
        price=price,
        vwap=vwap,
        pchange=pchange,
        day_high=high,
        day_low=low,
        open_price=open_price,
        range_pct=range_pct,
        provider="yfinance_us",
    )


def _pchange_from_ticker(symbol: str, last_price: float) -> float:
    if yf is None or last_price <= 0:
        return 0.0
    try:
        t = yf.Ticker(str(symbol).upper())
        dh = t.history(period="2d", interval="1d", auto_adjust=False)
        if dh is None or dh.empty:
            return 0.0
        dh = dh.dropna(subset=["Close"])
        if len(dh) >= 2:
            prev = float(dh.iloc[-2]["Close"] or 0.0)
        else:
            prev = float(dh.iloc[-1]["Close"] or 0.0)
        if prev <= 0:
            return 0.0
        return ((last_price - prev) / prev) * 100.0
    except Exception:
        return 0.0


def _daily_pchange_map(symbols: list[str]) -> dict[str, float]:
    """One yf.download for daily bars; compute % change vs prior close per symbol."""
    if yf is None or not symbols:
        return {}
    tickers = " ".join(str(s).upper() for s in symbols)
    daily = yf.download(
        tickers=tickers,
        period="5d",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    out: dict[str, float] = {}
    if daily is None or daily.empty:
        return out
    if isinstance(daily.columns, pd.MultiIndex):
        for sym in {str(c[0]) for c in daily.columns if isinstance(c, tuple)}:
            try:
                dsub = daily[sym].dropna(subset=["Close"])
                if dsub.empty:
                    continue
                if len(dsub) >= 2:
                    prev = float(dsub.iloc[-2]["Close"] or 0.0)
                    last = float(dsub.iloc[-1]["Close"] or 0.0)
                else:
                    prev = float(dsub.iloc[-1]["Close"] or 0.0)
                    last = prev
                if prev > 0:
                    out[sym.upper()] = ((last - prev) / prev) * 100.0
            except Exception:
                continue
    return out


def _flatten_cols(df: pd.DataFrame) -> None:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]


def us_quote_from_yfinance(symbol: str) -> Quote:
    """Single-symbol US quote (used when batching is not needed)."""
    if yf is None:
        raise ValueError("yfinance is not installed")
    t = yf.Ticker(str(symbol).upper())
    intra = t.history(period="1d", interval="1m", prepost=False, auto_adjust=False)
    if intra is None or intra.empty:
        intra = t.history(period="5d", interval="5m", prepost=False, auto_adjust=False)
    q = _intraday_bar_to_quote(symbol, intra)
    if q is None:
        raise ValueError(f"No US intraday market data for {symbol}")
    return q


def download_us_intraday_batch(
    symbols: list[str],
    *,
    interval: str = "1m",
    period: str = "1d",
) -> dict[str, Quote]:
    """Fetch intraday bars for multiple US tickers with one yf.download call."""
    if yf is None:
        raise ValueError("yfinance is not installed")
    if not symbols:
        return {}
    pchange_by_upper = _daily_pchange_map(symbols)
    tickers = " ".join(str(s).upper() for s in symbols)
    raw = yf.download(
        tickers=tickers,
        interval=interval,
        period=period,
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    if raw is None or raw.empty:
        return {}

    out: dict[str, Quote] = {}
    # MultiIndex columns: (Ticker, OHLCV)
    if isinstance(raw.columns, pd.MultiIndex):
        tickers_found = sorted(
            {str(c[0]) for c in raw.columns if isinstance(c, tuple) and len(c) > 1}
        )
        for sym in tickers_found:
            try:
                sub = raw[sym].copy()
                sub.columns = [str(c).strip() for c in sub.columns]
                orig = next(
                    (s for s in symbols if s.upper() == sym.upper()),
                    sym,
                )
                pc = pchange_by_upper.get(sym.upper())
                q = _intraday_bar_to_quote(orig, sub, pchange_override=pc)
                if q is not None:
                    out[orig] = q
            except Exception:
                continue
        return out

    # Single ticker flat frame
    if len(symbols) == 1:
        sub = raw.copy()
        _flatten_cols(sub)
        sub.columns = [str(c).strip() for c in sub.columns]
        pc = pchange_by_upper.get(symbols[0].upper())
        q = _intraday_bar_to_quote(symbols[0], sub, pchange_override=pc)
        if q is not None:
            out[symbols[0]] = q
    return out
