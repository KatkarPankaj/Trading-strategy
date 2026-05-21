"""Tests for unified AppSettings loader (phase 04)."""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stockmarket.config import TradingConfig
from stockmarket.settings import load_app_settings, to_trading_config

REPO_ROOT = Path(__file__).parent.parent


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def test_load_app_settings_from_repo_config():
    settings = load_app_settings(root=REPO_ROOT)
    assert "NSE" in settings.market
    assert "US" in settings.market
    assert settings.trading.starting_capital > 0
    assert isinstance(settings.app, dict)
    assert isinstance(settings.database, dict)


def test_nse_watchlist_parity_with_dashboard_inline():
    import dashboard_simple

    settings = load_app_settings(root=REPO_ROOT)
    inline = set(dashboard_simple.WATCHLIST_NSE)
    from_json = set(settings.market["NSE"].watchlist)
    assert inline == from_json


def test_nse_session_times_parity_with_dashboard():
    import dashboard_simple

    settings = load_app_settings(root=REPO_ROOT)
    nse = settings.market["NSE"]
    assert _hhmm(nse.market_open) == dashboard_simple.MARKET_CONFIG["NSE"]["market_open"]
    assert _hhmm(nse.entry_cutoff_time) == dashboard_simple.MARKET_CONFIG["NSE"]["entry_cutoff"]
    assert _hhmm(nse.square_off_time) == dashboard_simple.MARKET_CONFIG["NSE"]["square_off"]


def test_us_session_times_parity_with_dashboard():
    import dashboard_simple

    settings = load_app_settings(root=REPO_ROOT)
    us = settings.market["US"]
    assert _hhmm(us.market_open) == dashboard_simple.MARKET_CONFIG["US"]["market_open"]
    assert _hhmm(us.entry_cutoff_time) == dashboard_simple.MARKET_CONFIG["US"]["entry_cutoff"]
    assert _hhmm(us.square_off_time) == dashboard_simple.MARKET_CONFIG["US"]["square_off"]


def test_to_trading_config_matches_direct_load():
    settings = load_app_settings(root=REPO_ROOT)
    direct = TradingConfig.from_json(REPO_ROOT / "config.json")
    loaded = to_trading_config(settings)
    assert loaded == direct
