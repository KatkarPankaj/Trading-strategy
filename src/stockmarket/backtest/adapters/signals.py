"""Historical signal source from strategy-enriched bars."""

from __future__ import annotations

import pandas as pd

from stockmarket.cycle.ports import RankedSignals
from stockmarket.domain.types import CycleSettings, PaperState


class HistoricalSignalSource:
    def __init__(self, symbol: str, row: pd.Series):
        self._symbol = symbol
        self._row = row

    def set_bar(self, row: pd.Series) -> None:
        self._row = row

    def rank(self, state: PaperState, settings: CycleSettings) -> RankedSignals:
        row = self._row
        sym = self._symbol
        price = float(row.get("close", 0.0) or 0.0)
        buy_rows: list[dict] = []
        sell_rows: list[dict] = []

        if bool(row.get("long_signal", False)):
            buy_rows.append(
                {
                    "symbol": sym,
                    "price": price,
                    "buy_signal": "READY",
                    "buy_score": 100.0,
                    "effective_buy_score": 100.0,
                }
            )
        if bool(row.get("short_signal", False)):
            sell_rows.append(
                {
                    "symbol": sym,
                    "price": price,
                    "sell_signal": "READY",
                    "sell_score": 100.0,
                    "effective_sell_score": 100.0,
                }
            )

        buy_df = pd.DataFrame(buy_rows) if buy_rows else pd.DataFrame()
        sell_df = pd.DataFrame(sell_rows) if sell_rows else pd.DataFrame()
        return RankedSignals(buy_df, sell_df, pd.DataFrame())
