"""Quote parsing and TTL cache behavior."""

from __future__ import annotations

import pytest

from stockmarket.quotes.nse import quote_from_price_info
from stockmarket.quotes.service import QuoteService
from stockmarket.quotes.ttl_cache import TtlCache


def test_quote_from_nse_payload_minimal():
    payload = {
        "priceInfo": {
            "lastPrice": 100.0,
            "vwap": 99.0,
            "pChange": 1.5,
            "intraDayHighLow": {"min": 98.0, "max": 102.0},
            "open": 99.5,
        }
    }
    q = quote_from_price_info("RELIANCE.NS", payload)
    assert q.price == 100.0
    assert q.vwap == 99.0
    assert q.pchange == 1.5
    assert q.day_low == 98.0
    assert q.day_high == 102.0
    d_simple = q.to_simple_dict()
    assert d_simple["price"] == 100.0
    d_complex = q.to_complex_nse_dict()
    assert d_complex["last_price"] == 100.0
    assert d_complex["day_range_pct"] > 0


def test_ttl_cache_reuses_value():
    c: TtlCache[str, int] = TtlCache(1.0)
    calls = {"n": 0}

    def factory() -> int:
        calls["n"] += 1
        return 42

    assert c.get_or_set("a", factory) == 42
    assert c.get_or_set("a", factory) == 42
    assert calls["n"] == 1


def test_quote_service_nse_uses_injected_fetch(monkeypatch):
    def fake_fetch(url: str):
        return {
            "priceInfo": {
                "lastPrice": 10.0,
                "vwap": 9.8,
                "pChange": 0.4,
                "intraDayHighLow": {"min": 9.5, "max": 10.2},
            }
        }

    svc = QuoteService(nse_ttl_sec=60.0, nsefetch=lambda u: fake_fetch(u))
    q1 = svc.get_nse_quote("X.NS")
    q2 = svc.get_nse_quote("X.NS")
    assert q1.price == q2.price == 10.0


def test_quote_service_nse_quotes_parallel_fetches(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url: str):
        sym = url.split("symbol=")[-1].strip()
        calls.append(sym)
        return {
            "priceInfo": {
                "lastPrice": float(len(sym)),
                "vwap": 1.0,
                "pChange": 0.1,
                "intraDayHighLow": {"min": 1.0, "max": 2.0},
            }
        }

    syms = ["A.NS", "AB.NS", "ABC.NS"]
    svc = QuoteService(nse_ttl_sec=60.0, nsefetch=lambda u: fake_fetch(u), nse_max_workers=3)
    m = svc.get_nse_quotes(syms)
    assert set(m.keys()) == set(syms)
    assert m["A.NS"].price == 1.0
    assert m["AB.NS"].price == 2.0
    assert m["ABC.NS"].price == 3.0
    assert sorted(calls) == ["A", "AB", "ABC"]


def test_quote_service_us_batch_called_once(monkeypatch):
    import stockmarket.quotes.yahoo_quotes as yq
    from stockmarket.quotes.types import Quote

    called = {"n": 0}

    def fake_batch(symbols: list[str], **kwargs):
        called["n"] += 1
        return {
            "AAPL": Quote(
                symbol="AAPL",
                price=100.0,
                vwap=99.0,
                pchange=1.0,
                day_high=101.0,
                day_low=98.0,
                range_pct=2.0,
                provider="t",
            )
        }

    monkeypatch.setattr(yq, "download_us_intraday_batch", fake_batch)
    svc = QuoteService(us_ttl_sec=60.0)
    svc._us_cache.clear()
    m1 = svc.get_us_quotes(["AAPL"])
    m2 = svc.get_us_quotes(["AAPL"])
    assert "AAPL" in m1
    assert called["n"] == 1
