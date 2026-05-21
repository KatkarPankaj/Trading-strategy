"""Unified application settings (trading + market + app + database)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stockmarket.config import TradingConfig
from stockmarket.utils.config_loader import ConfigLoader


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MarketProfile:
    name: str
    currency: str
    timezone: str
    market_open: str
    market_close: str
    entry_cutoff_time: str
    square_off_time: str
    watchlist: tuple[str, ...]
    intraday_charges: dict[str, Any]


@dataclass(frozen=True)
class AppSettings:
    trading: TradingConfig
    market: dict[str, MarketProfile]
    app: dict[str, Any]
    database: dict[str, Any]


def _profile_from_raw(key: str, raw: dict[str, Any]) -> MarketProfile:
    return MarketProfile(
        name=str(raw.get("name", key)),
        currency=str(raw.get("currency", "")),
        timezone=str(raw["timezone"]),
        market_open=str(raw["market_open"]),
        market_close=str(raw["market_close"]),
        entry_cutoff_time=str(raw["entry_cutoff_time"]),
        square_off_time=str(raw["square_off_time"]),
        watchlist=tuple(raw.get("watchlist", [])),
        intraday_charges=dict(raw.get("intraday_charges", {})),
    )


def load_app_settings(
    root: Path | None = None,
    trading_path: Path | None = None,
    config_dir: Path | None = None,
) -> AppSettings:
    root = root or repo_root()
    trading_path = trading_path or root / "config.json"
    config_dir = config_dir or root / "config"
    loader = ConfigLoader(config_dir)
    trading = TradingConfig.from_json(trading_path)
    markets_raw = loader.get_market_config()
    market = {k: _profile_from_raw(k, v) for k, v in markets_raw.items()}
    return AppSettings(
        trading=trading,
        market=market,
        app=loader.get_app_config(),
        database=loader.get_database_config(),
    )


def to_trading_config(settings: AppSettings) -> TradingConfig:
    return settings.trading
