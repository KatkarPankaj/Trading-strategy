"""Canonical quote representation for dashboards and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Quote:
    """Unified quote: use `price` as last traded price."""

    symbol: str
    price: float
    vwap: float = 0.0
    pchange: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    open_price: float = 0.0
    range_pct: float = 0.0
    provider: str = ""
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def to_simple_dict(self) -> dict:
        """dashboard_simple-style keys."""
        return {
            "symbol": self.symbol,
            "price": self.price,
            "vwap": self.vwap,
            "pchange": self.pchange,
            "range_pct": self.range_pct,
        }

    def to_complex_nse_dict(self) -> dict:
        """dashboard.py NSE quote dict (last_price + day_range_pct)."""
        return {
            "symbol": self.symbol,
            "last_price": self.price,
            "open": self.open_price,
            "vwap": self.vwap,
            "pchange": self.pchange,
            "day_low": self.day_low,
            "day_high": self.day_high,
            "day_range_pct": (
                (self.day_high - self.day_low) / max(self.price, 1e-6) * 100
                if self.price > 0
                else 0.0
            ),
        }
