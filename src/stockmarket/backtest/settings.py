"""Map TradingConfig to CycleSettings for backtest cycle runs."""

from __future__ import annotations

from datetime import time

from stockmarket.config import TradingConfig
from stockmarket.domain.types import (
    CycleSettings,
    GuardSettings,
    RiskSettings,
    SignalSettings,
)


def cycle_settings_from_trading_config(
    cfg: TradingConfig, symbols: list[str]
) -> CycleSettings:
    risk_pct = float(cfg.risk_per_trade_pct) * 100.0
    return CycleSettings(
        risk=RiskSettings(
            risk_pct=risk_pct,
            sl_pct=float(cfg.stop_loss_pct),
            tp_pct=float(cfg.take_profit_pct),
            max_trades_day=int(cfg.max_trades_per_day),
            max_positions=1,
            max_qty_per_trade=10_000,
            max_symbol_allocation_pct=100.0,
            max_total_deployment_pct=100.0,
            max_trade_invest_pct=100.0,
            min_order_value=0.0,
            min_price=0.0,
            max_price=1e12,
        ),
        signals=SignalSettings(
            min_buy_score=0.0,
            min_sell_score=0.0,
            min_short_score=0.0,
            enable_signal_sell=False,
            enable_short_selling=bool(cfg.allow_short),
            max_signal_exits_per_cycle=0,
            idle_buy_fallback_minutes=999_999,
        ),
        guards=GuardSettings(
            enable_profit_guard=False,
            profit_guard_drawdown_pct=100.0,
            profit_guard_after=time(23, 59),
            block_new_entries_on_guard=False,
            daily_profit_target=0.0,
            reentry_cooldown_minutes=0,
            reentry_min_move_pct=0.0,
            sl_cooldown_after_stop_minutes=0,
            enable_regime_entry_gate=False,
            market_regime="unknown",
        ),
        symbols=tuple(symbols),
    )
