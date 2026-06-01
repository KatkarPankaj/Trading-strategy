"""Tests for live market status (NSE API, Finnhub) and config fallback."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stockmarket.market_status import (
    MARKET_DISPLAY_LABELS,
    clear_market_status_cache,
    fetch_nse_session_status,
    fetch_us_session_status,
    is_regular_session_open,
    resolve_all_market_statuses,
)
from stockmarket.quotes.nse import is_nse_capital_market_open
from stockmarket.settings import load_app_settings

REPO_ROOT = Path(__file__).parent.parent


def _nse_open_payload() -> dict:
    return {
        "marketState": [
            {"market": "Capital Market", "marketStatus": "Open"},
            {"market": "Currency", "marketStatus": "Close"},
        ]
    }


def test_is_nse_capital_market_open_true():
    assert is_nse_capital_market_open(_nse_open_payload()) is True


def test_is_nse_capital_market_open_closed():
    payload = {
        "marketState": [{"market": "Capital Market", "marketStatus": "Close"}]
    }
    assert is_nse_capital_market_open(payload) is False


def test_is_regular_session_open_weekday_mid_session():
    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    now = datetime(2026, 5, 26, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert is_regular_session_open(nse, now=now) is True


def test_is_regular_session_open_weekend():
    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    now = datetime(2026, 5, 23, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert is_regular_session_open(nse, now=now) is False


def test_is_regular_session_open_us_after_close():
    settings = load_app_settings(root=REPO_ROOT)
    us = settings.market["US"]
    now = datetime(2026, 5, 26, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    assert is_regular_session_open(us, now=now) is False


def test_fetch_nse_session_status_api(caplog):
    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    caplog.set_level("INFO", logger="stockmarket.market_status")

    status = fetch_nse_session_status(
        nse,
        MARKET_DISPLAY_LABELS["NSE"],
        nsefetch=lambda _url: _nse_open_payload(),
    )
    assert status.is_open is True
    assert status.source == "api"
    assert "fetched from NSE API" in caplog.text


def test_fetch_nse_session_status_empty_payload_fallback(caplog):
    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    caplog.set_level("WARNING", logger="stockmarket.market_status")

    status = fetch_nse_session_status(
        nse,
        MARKET_DISPLAY_LABELS["NSE"],
        nsefetch=lambda _url: {},
    )
    assert status.source == "fallback"
    assert "config hours fallback" in caplog.text


def test_fetch_nse_session_status_fallback(caplog):
    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    caplog.set_level("WARNING", logger="stockmarket.market_status")

    def _boom(_url):
        raise RuntimeError("network down")

    status = fetch_nse_session_status(
        nse,
        MARKET_DISPLAY_LABELS["NSE"],
        nsefetch=_boom,
    )
    assert status.source == "fallback"
    assert "config hours fallback" in caplog.text


@patch("stockmarket.market_status.fetch_market_status")
def test_fetch_us_session_status_api(mock_fetch, caplog):
    settings = load_app_settings(root=REPO_ROOT)
    us = settings.market["US"]
    mock_fetch.return_value = {"isOpen": True, "exchange": "US"}
    caplog.set_level("INFO", logger="stockmarket.market_status")

    status = fetch_us_session_status(us, MARKET_DISPLAY_LABELS["US"])
    assert status.is_open is True
    assert status.source == "api"
    assert "fetched from Finnhub" in caplog.text


@patch("stockmarket.market_status.fetch_market_status")
def test_fetch_us_session_status_fallback(mock_fetch, caplog):
    settings = load_app_settings(root=REPO_ROOT)
    us = settings.market["US"]
    mock_fetch.side_effect = ValueError("no key")
    caplog.set_level("WARNING", logger="stockmarket.market_status")

    status = fetch_us_session_status(us, MARKET_DISPLAY_LABELS["US"])
    assert status.source == "fallback"
    assert "config hours fallback" in caplog.text


@patch("stockmarket.market_status.fetch_market_status")
def test_resolve_all_force_selected_refetches(mock_fetch):
    settings = load_app_settings(root=REPO_ROOT)
    mock_fetch.return_value = {"isOpen": False}
    session: dict = {}

    def nsefetch(_url):
        return _nse_open_payload()

    resolve_all_market_statuses(
        settings.market,
        MARKET_DISPLAY_LABELS,
        selected_market="US",
        session=session,
        nsefetch=nsefetch,
    )
    assert mock_fetch.call_count == 1

    resolve_all_market_statuses(
        settings.market,
        MARKET_DISPLAY_LABELS,
        selected_market="NSE",
        session=session,
        nsefetch=nsefetch,
    )
    assert mock_fetch.call_count == 1

    clear_market_status_cache(session)
    resolve_all_market_statuses(
        settings.market,
        MARKET_DISPLAY_LABELS,
        selected_market="US",
        session=session,
        nsefetch=nsefetch,
    )
    assert mock_fetch.call_count == 2


def test_clear_market_status_cache():
    session = {"s_market_status_cache": {"NSE": {"is_open": True}}}
    clear_market_status_cache(session)
    assert "s_market_status_cache" not in session
