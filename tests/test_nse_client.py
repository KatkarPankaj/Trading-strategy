"""Hardened NSE client and feed validation."""

from __future__ import annotations

import pytest

from stockmarket.quotes.nse_client import (
    NseFetchError,
    HardenedNseClient,
    hardened_nsefetch,
    is_valid_market_status_payload,
    is_valid_quote_equity_payload,
)
from stockmarket.quotes.service import QuoteService


def test_is_valid_quote_equity_payload():
    assert is_valid_quote_equity_payload(
        {"priceInfo": {"lastPrice": 100.0}}
    )
    assert not is_valid_quote_equity_payload({})
    assert not is_valid_quote_equity_payload({"priceInfo": {"lastPrice": 0}})


def test_is_valid_market_status_payload():
    assert is_valid_market_status_payload(
        {"marketState": [{"market": "Capital Market", "marketStatus": "Open"}]}
    )
    assert not is_valid_market_status_payload({})


def test_hardened_nsefetch_raises_on_empty(monkeypatch):
    client = HardenedNseClient()

    def _empty(_url, *, referer=None):
        return {}

    monkeypatch.setattr(client, "fetch_json", _empty)
    monkeypatch.setattr(
        "stockmarket.quotes.nse_client.get_hardened_nse_client",
        lambda: client,
    )
    with pytest.raises(NseFetchError):
        hardened_nsefetch(
            "https://www.nseindia.com/api/quote-equity?symbol=SBIN"
        )


def test_quote_service_reports_batch_failure(monkeypatch):
    def _bad_fetch(_url: str):
        return {}

    svc = QuoteService(nse_ttl_sec=60.0, nsefetch=_bad_fetch)
    out = svc.get_nse_quotes(["X.NS", "Y.NS"])
    assert out == {}
    diag = svc.last_nse_batch_diag
    assert diag is not None
    assert diag.with_price == 0
    assert "unavailable" in diag.message.lower()
