"""Position sizing aligned with legacy backtest._position_size."""

from __future__ import annotations

from stockmarket.config import TradingConfig
from stockmarket.domain.types import CycleSettings, PaperState


class BacktestPositionSizer:
    def __init__(self, cfg: TradingConfig):
        self._cfg = cfg


    def _capital(self, state: PaperState) -> float:
        return float(state.ui_config.get("_risk_capital", state.start_capital))

    def _qty(self, entry_price: float, state: PaperState) -> int:
        cfg = self._cfg
        capital = self._capital(state)
        per_share_risk = entry_price * cfg.stop_loss_pct
        if per_share_risk <= 0:
            return 0
        risk_budget = capital * cfg.risk_per_trade_pct
        qty = int(risk_budget // per_share_risk)
        return max(0, qty)

    def size_long(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int:
        slipped = float(price) * (1.0 + float(self._cfg.slippage_pct))
        return self._qty(slipped, state)

    def size_short(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int:
        slipped = float(price) * (1.0 - float(self._cfg.slippage_pct))
        return self._qty(slipped, state)
