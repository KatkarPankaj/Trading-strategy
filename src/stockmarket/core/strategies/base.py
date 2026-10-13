"""Shared contract for deterministic, non-executable strategy outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any
from typing import Protocol, runtime_checkable

import pandas as pd

from ..market_session import MarketSession
from ..models import AssetClass, Instrument, Signal


def strategy_parameters(strategy: Any) -> dict[str, Any] | None:
    config = getattr(strategy, "config", None)
    if config is None or not is_dataclass(config) or isinstance(config, type):
        return None
    return {k: v.total_seconds() if isinstance(v, timedelta) else v
            for k, v in asdict(config).items()}


def strategy_parameters_hash(strategy: Any) -> str | None:
    parameters = strategy_parameters(strategy)
    if parameters is None:
        return None
    return hashlib.sha256(json.dumps(
        parameters, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()


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


class PaperStrategyVersion:
    """Trusted immutable template bound to one evaluated instrument and version."""

    def __init__(self, template: Strategy, *, name: str, version: str,
                 instrument_id: str, market: str, available_from: datetime) -> None:
        self.template = template
        self.name = name
        self.version = version
        self.instrument_id = instrument_id
        self.available_from = available_from
        self.config = getattr(template, "config")
        metadata = getattr(template, "metadata")
        if not isinstance(metadata, StrategyMetadata):
            raise ValueError("template metadata is required for a paper version")
        self.metadata = StrategyMetadata(
            version, frozenset({market}), metadata.supported_asset_classes)

    def evaluate(self, instrument: Instrument, bars: pd.DataFrame,
                 session: MarketSession, *, as_of: datetime) -> Signal:
        if instrument.instrument_id != self.instrument_id:
            raise ValueError("paper strategy version was evaluated for a different instrument")
        return replace(self.template.evaluate(instrument, bars, session, as_of=as_of),
                       strategy=self.name)
