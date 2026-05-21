"""Signal ranking adapter delegating to dashboard helpers."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from stockmarket.domain.types import CycleSettings, PaperState

from ..ports import RankedSignals


class DashboardSignalSource:
    def __init__(self, rank_fn: Callable[..., tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list]]):
        self._rank_fn = rank_fn

    def rank(self, state: PaperState, settings: CycleSettings) -> RankedSignals:
        risk = settings.risk
        buy_df, sell_df, sell_exit_df, _ = self._rank_fn(
            symbols=list(settings.symbols),
            min_price=risk.min_price,
            max_price=risk.max_price,
            risk_pct=risk.risk_pct,
            sl_pct=risk.sl_pct,
            tp_pct=risk.tp_pct,
            max_symbol_allocation_pct=risk.max_symbol_allocation_pct,
            max_total_deployment_pct=risk.max_total_deployment_pct,
            max_qty_per_trade=risk.max_qty_per_trade,
            max_open_positions=risk.max_positions,
            min_order_value=risk.min_order_value,
            max_trade_invest_pct=risk.max_trade_invest_pct,
        )
        return RankedSignals(buy_df=buy_df, sell_df=sell_df, sell_exit_df=sell_exit_df)


class StaticSignalSource:
    """Fixed signal frames for unit tests."""

    def __init__(
        self,
        buy_df: pd.DataFrame | None = None,
        sell_df: pd.DataFrame | None = None,
        sell_exit_df: pd.DataFrame | None = None,
    ):
        import pandas as pd

        empty = pd.DataFrame()
        self._buy = buy_df if buy_df is not None else empty
        self._sell = sell_df if sell_df is not None else empty
        self._exit = sell_exit_df if sell_exit_df is not None else empty

    def rank(self, state: PaperState, settings: CycleSettings) -> RankedSignals:
        return RankedSignals(
            buy_df=self._buy.copy(),
            sell_df=self._sell.copy(),
            sell_exit_df=self._exit.copy(),
        )
