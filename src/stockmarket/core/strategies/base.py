"""Shared contract for deterministic, non-executable strategy outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from ..market_session import MarketSession
from ..models import AssetClass, Instrument, Signal


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    """Version and compatibility contract required for persisted signal generation."""

    version: str
    supported_markets: frozenset[str]
    supported_asset_classes: frozenset[AssetClass]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("strategy version must be a non-empty string")
        if not self.supported_markets or any(
                not isinstance(market, str) or not market.strip()
                for market in self.supported_markets):
            raise ValueError("strategy metadata requires supported markets")
        if not self.supported_asset_classes or any(
                not isinstance(asset_class, AssetClass)
                for asset_class in self.supported_asset_classes):
            raise ValueError("strategy metadata requires supported asset classes")


@runtime_checkable
class Strategy(Protocol):
    """Evaluate validated market bars and return a domain signal, never an order."""

    @property
    def name(self) -> str: ...

    def evaluate(
        self,
        instrument: Instrument,
        bars: pd.DataFrame,
        session: MarketSession,
        *,
        as_of: datetime,
    ) -> Signal: ...
