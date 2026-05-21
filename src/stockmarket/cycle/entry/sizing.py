"""Position sizing for cycle entries."""

from __future__ import annotations

from stockmarket.domain.types import CycleSettings, PaperState


class DefaultPositionSizer:
    def size_long(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int:
        risk = settings.risk
        risk_budget = float(state.start_capital) * (float(risk.risk_pct) / 100.0)
        max_qty_limit = max(1, int(risk.max_qty_per_trade))
        max_invest = float(state.start_capital) * (
            float(risk.max_total_deployment_pct) / 100.0
        ) * (float(risk.max_trade_invest_pct) / 100.0)
        risk_per_share = max(float(price) * float(risk.sl_pct), 0.01)
        qty_by_risk = int(risk_budget // risk_per_share)
        qty_by_cash = int(state.cash // max(float(price), 1e-6))
        qty_by_trade_cap = int(max_invest // max(float(price), 1e-6))
        return max(0, min(qty_by_risk, qty_by_cash, qty_by_trade_cap, max_qty_limit))

    def size_short(
        self, *, price: float, state: PaperState, settings: CycleSettings
    ) -> int:
        risk = settings.risk
        risk_budget = float(state.start_capital) * (float(risk.risk_pct) / 100.0)
        max_qty_limit = max(1, int(risk.max_qty_per_trade))
        max_invest = float(state.start_capital) * (
            float(risk.max_total_deployment_pct) / 100.0
        ) * (float(risk.max_trade_invest_pct) / 100.0)
        risk_per_share = max(float(price) * float(risk.sl_pct), 0.01)
        qty_by_risk = int(risk_budget // risk_per_share)
        qty_by_margin = int(max(0.0, state.cash) // max(float(price) * 0.2, 0.01))
        qty_by_trade_cap = int(max_invest // max(float(price), 1e-6))
        return max(0, min(qty_by_risk, qty_by_margin, qty_by_trade_cap, max_qty_limit))
