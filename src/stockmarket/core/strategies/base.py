"""Shared contract for deterministic, non-executable strategy outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from ..market_session import MarketSession
from ..models import Instrument, Signal


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
