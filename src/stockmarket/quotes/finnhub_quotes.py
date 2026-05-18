"""US equity quotes via Finnhub (candles + quote)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd

from ..finnhub_client import fetch_candles, fetch_intraday_candles_df_params, fetch_quote
from .types import Quote


def _candles_df_to_quote(
    symbol: str,
    intraday: pd.DataFrame,
    *,
    pchange_override: float | None = None,
) -> Quote | None:
    if intraday is None or intraday.empty:
        return None
    intra = intraday.dropna(subset=["close"]).copy()
    if intra.empty:
        return None
    intra.columns = [str(c).strip().lower() for c in intra.columns]

    latest = intra.iloc[-1]
    price = float(latest.get("close") or 0.0)
    high = float(intra["high"].max() or price)
    low = float(intra["low"].min() or price)
    open_price = float(intra.iloc[0].get("open") or price)

    vs = intra["volume"]
    if float(vs.fillna(0).sum()) > 0:
        c = intra["close"].fillna(0.0)
        vwap = float((c * vs.fillna(0.0)).sum() / max(float(vs.fillna(0.0).sum()), 1e-6))
    else:
        vwap = (high + low + open_price + price) / 4.0 if price > 0 else 0.0

    range_pct = ((high - low) / max(price, 1e-6)) * 100.0 if price > 0 else 0.0
    pchange = float(pchange_override) if pchange_override is not None else 0.0
    return Quote(
        symbol=symbol,
        price=price,
        vwap=vwap,
        pchange=pchange,
        day_high=high,
        day_low=low,
        open_price=open_price,
        range_pct=range_pct,
        provider="finnhub_us",
    )


def _finnhub_candles_to_df(payload: dict[str, Any]) -> pd.DataFrame:
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        return pd.DataFrame()
    ts = payload.get("t") or []
    if not ts:
        return pd.DataFrame()
    o = payload.get("o") or []
    h = payload.get("h") or []
    low = payload.get("l") or []
    c = payload.get("c") or []
    v = payload.get("v") or []
    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True)
    df = pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": v},
        index=idx,
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(how="all")


def us_quote_from_finnhub(symbol: str) -> Quote:
    sym = str(symbol).upper().strip()
    q = fetch_quote(sym)
    price = float(q.get("c") or 0.0)
    prev = float(q.get("pc") or 0.0)
    pchange = ((price - prev) / max(prev, 1e-6)) * 100.0 if prev > 0 else 0.0

    res, from_u, to_u = fetch_intraday_candles_df_params(sym, "5m", "5d")
    raw = fetch_candles(sym, resolution=res, from_unix=from_u, to_unix=to_u)
    df = _finnhub_candles_to_df(raw)
    if df.empty:
        o = float(q.get("o") or price)
        h = float(q.get("h") or price)
        l = float(q.get("l") or price)
        vwap = (o + h + l + price) / 4.0 if price > 0 else 0.0
        rng = ((h - l) / max(price, 1e-6)) * 100.0 if price > 0 else 0.0
        return Quote(
            symbol=sym,
            price=price,
            vwap=vwap,
            pchange=pchange,
            day_high=h,
            day_low=l,
            open_price=o,
            range_pct=rng,
            provider="finnhub_us",
        )
    out = _candles_df_to_quote(sym, df, pchange_override=pchange)
    if out is None:
        raise ValueError(f"No US Finnhub market data for {sym}")
    return out


def _one_symbol_batch(sym: str, interval: str, period: str) -> tuple[str, Quote | None]:
    try:
        sym_u = str(sym).upper().strip()
        qjson = fetch_quote(sym_u)
        pchange = 0.0
        pc = float(qjson.get("pc") or 0.0)
        c = float(qjson.get("c") or 0.0)
        if pc > 0 and c > 0:
            pchange = ((c - pc) / pc) * 100.0

        res, from_u, to_u = fetch_intraday_candles_df_params(sym_u, interval, period)
        raw = fetch_candles(sym_u, resolution=res, from_unix=from_u, to_unix=to_u)
        df = _finnhub_candles_to_df(raw)
        quote = _candles_df_to_quote(sym_u, df, pchange_override=pchange)
        return sym, quote
    except Exception:
        return sym, None


def download_us_intraday_batch(
    symbols: list[str],
    *,
    interval: str = "1m",
    period: str = "1d",
) -> dict[str, Quote]:
    if not symbols:
        return {}
    out: dict[str, Quote] = {}
    workers = min(12, len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_one_symbol_batch, s, interval, period): s for s in symbols
        }
        for fut in as_completed(futs):
            sym, q = fut.result()
            if q is not None:
                out[sym] = q
    return out
