"""Cycle pipeline ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Literal, Protocol

import pandas as pd

from stockmarket.domain.types import CycleSettings, PaperState, Side, TradeLogEntry

Step = Callable[["CycleContext", "Services"], "CycleContext"]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def in_entry_window(self, now: datetime) -> bool: ...

    def square_off_time(self) -> time: ...

    def market_open_time(self) -> time: ...


class Broker(Protocol):
    def execute(
        self,
        state: PaperState,
        *,
        symbol: str,
        side: Side,
        qty: int,
        price: float,
        reason: str,
        when: datetime,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
    ) -> TradeLogEntry | None: ...


@dataclass(frozen=True)
class RankedSignals:
    buy_df: pd.DataFrame
    sell_df: pd.DataFrame
    sell_exit_df: pd.DataFrame


class SignalSource(Protocol):
    def rank(self, state: PaperState, settings: CycleSettings) -> RankedSignals: ...


class HistoryQuery(Protocol):
    def latest_stop_loss_by_symbol(self, state: PaperState) -> dict[str, tuple[datetime, str]]: ...

    def latest_exit_by_symbol(
        self, state: PaperState, day: str
    ) -> dict[str, tuple[datetime, float]]: ...

    def minutes_since_last_entry(
        self, state: PaperState, now: datetime, day: str
    ) -> float: ...

    def today_entry_count(self, state: PaperState, day: str) -> int: ...

    def current_open_pnl(self, state: PaperState) -> float: ...


class PriceRefresh(Protocol):
    def refresh(self, state: PaperState) -> None: ...


class CooldownPolicy(Protocol):
    def block_reason(
        self,
        symbol: str,
        *,
        side: Literal["long", "short"],
        ref_price: float,
        sl_events: dict[str, tuple[datetime, str]],
        last_exits: dict[str, tuple[datetime, float]],
        now_naive: datetime,
        settings: CycleSettings,
    ) -> str | None: ...


class PositionSizer(Protocol):
    def size_long(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int: ...

    def size_short(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int: ...


class IdleFallbackPolicy(Protocol):
    def allow_below_min_score(
        self, *, idle_minutes: float, settings: CycleSettings
    ) -> bool: ...
