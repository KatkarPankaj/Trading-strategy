"""Central quote service: NSE (parallel + TTL) and US (Finnhub)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .nse import (
    fetch_nse_quote_equity_raw,
    quote_from_price_info,
    to_nse_symbol,
)
from .ttl_cache import TtlCache
from .types import Quote
from . import finnhub_quotes

try:
    from nsepython import nsefetch as _default_nsefetch
except Exception:  # pragma: no cover
    _default_nsefetch = None  # type: ignore


class QuoteService:
    """Fetches quotes with a process-local TTL cache (streamlit cache can wrap methods)."""

    def __init__(
        self,
        *,
        nse_ttl_sec: float = 15.0,
        us_ttl_sec: float = 15.0,
        nsefetch: Callable[..., Any] | None = None,
        nse_max_workers: int = 6,
    ) -> None:
        self._nse_fetch = nsefetch or _default_nsefetch
        self._nse_max_workers = max(1, int(nse_max_workers))
        self._nse_cache: TtlCache[str, Quote] = TtlCache(nse_ttl_sec)
        self._us_cache: TtlCache[str, Quote] = TtlCache(us_ttl_sec)

    def _load_nse_quote_from_network(self, symbol: str) -> Quote:
        if self._nse_fetch is None:
            raise ValueError("nsepython is not installed. Run: pip install nsepython")
        nse_sym = to_nse_symbol(symbol)
        data = fetch_nse_quote_equity_raw(nse_sym, self._nse_fetch)
        return quote_from_price_info(symbol, data, provider="nse")

    def get_nse_quote(self, symbol: str) -> Quote:
        key = f"nse:{symbol.upper()}"

        def _load() -> Quote:
            return self._load_nse_quote_from_network(symbol)

        return self._nse_cache.get_or_set(key, _load)

    def get_nse_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        out: dict[str, Quote] = {}
        need: list[str] = []
        for s in symbols:
            k = f"nse:{s.upper()}"
            q = self._nse_cache.get(k)
            if q is not None:
                out[s] = q
            else:
                need.append(s)
        if not need:
            return out
        if self._nse_fetch is None:
            return out

        def fetch_one(sym: str) -> tuple[str, Quote | None]:
            for attempt in range(3):
                try:
                    q = self._load_nse_quote_from_network(sym)
                    return sym, q
                except Exception as e:
                    err_text = str(e)
                    low = err_text.lower()
                    if "429" in err_text or "rate limit" in low:
                        time.sleep(0.25 * (2**attempt))
                        continue
                    return sym, None
            return sym, None

        workers = min(self._nse_max_workers, len(need))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_one, s) for s in need]
            for fut in as_completed(futures):
                sym, q = fut.result()
                if q is not None:
                    self._nse_cache.set(f"nse:{sym.upper()}", q)
                    out[sym] = q
        return out

    def get_us_quote(self, symbol: str) -> Quote:
        key = f"us:{symbol.upper()}"

        def _load() -> Quote:
            return finnhub_quotes.us_quote_from_finnhub(symbol)

        return self._us_cache.get_or_set(key, _load)

    def get_us_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        out: dict[str, Quote] = {}
        need_batch: list[str] = []
        for s in symbols:
            k = f"us:{s.upper()}"
            q = self._us_cache.get(k)
            if q is not None:
                out[s] = q
            else:
                need_batch.append(s)
        if need_batch:
            batch = finnhub_quotes.download_us_intraday_batch(need_batch)
            for sym, q in batch.items():
                self._us_cache.set(f"us:{sym.upper()}", q)
            for s in need_batch:
                q2 = self._us_cache.get(f"us:{s.upper()}")
                if q2 is not None:
                    out[s] = q2
        return out


# Shared instance for apps that want process-wide cache (optional)
_default_service: QuoteService | None = None


def get_default_quote_service() -> QuoteService:
    global _default_service
    if _default_service is None:
        _default_service = QuoteService()
    return _default_service
