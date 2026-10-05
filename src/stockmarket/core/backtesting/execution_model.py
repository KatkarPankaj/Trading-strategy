"""Execution realism: spread, slippage, commission, latency, partial fills, and corporate actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from math import floor, isfinite

from ..models import OrderSide


class PriceBasis(str, Enum):
    RAW = "RAW"  # unadjusted prices; corporate actions are applied to positions
    # prices already adjusted; actions are ignored to avoid double counting
    ADJUSTED = "ADJUSTED"


class ActionKind(str, Enum):
    SPLIT = "SPLIT"  # value = new shares per old share (2.0 for a 2-for-1)
    DIVIDEND = "DIVIDEND"  # value = cash per share


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: str
    ex_date: date  # instrument-local date; effective from that date's first bar
    kind: ActionKind
    value: float

    def __post_init__(self) -> None:
        if not isfinite(self.value) or self.value <= 0:
            raise ValueError(
                "corporate action value must be finite and positive")


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    commission_rate: float = 0.0
    min_commission: float = 0.0
    fixed_commission: float = 0.0
    spread_bps: float = 0.0  # full quoted spread; half is paid per side
    slippage_bps: float = 0.0
    # orders act on a later bar than the decision; same-bar fills are not allowed
    latency_bars: int = 1
    # max share of a bar's volume per order
    participation_rate: float | None = None
    max_order_bars: int = 5  # unfilled remainder is cancelled after this many bars

    def __post_init__(self) -> None:
        for name in ("commission_rate", "min_commission", "fixed_commission", "spread_bps", "slippage_bps"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.latency_bars < 1:
            raise ValueError(
                "latency_bars must be at least 1 to avoid look-ahead")
        if self.max_order_bars < 1:
            raise ValueError("max_order_bars must be at least 1")
        if self.participation_rate is not None and not 0 < self.participation_rate <= 1:
            raise ValueError("participation_rate must be in (0, 1]")

    def fill_price(self, price: float, side: OrderSide) -> float:
        adverse = (self.spread_bps / 2 + self.slippage_bps) / 10_000
        return price * (1 + adverse) if side is OrderSide.BUY else price * (1 - adverse)

    def commission(self, notional: float) -> float:
        return max(self.min_commission, self.commission_rate * notional) + self.fixed_commission

    def fillable_quantity(self, remaining: int, bar_volume: float) -> int:
        if self.participation_rate is None:
            return remaining
        return min(remaining, int(floor(self.participation_rate * bar_volume)))
