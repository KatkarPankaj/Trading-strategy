"""Typed paper-trading domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Literal

Side = Literal["BUY", "SELL", "SHORT", "COVER"]


@dataclass(frozen=True)
class Position:
    qty: int
    avg: float
    stop: float
    target: float


@dataclass(frozen=True)
class TradeLogEntry:
    ts: str
    symbol: str
    side: Side
    qty: int
    price: float
    charges: float
    realized_delta: float
    reason: str
    cash_after: float


@dataclass
class PaperState:
    start_capital: float
    cash: float
    realized: float
    charges: float
    holdings: dict[str, Position]
    shorts: dict[str, Position]
    prices: dict[str, float]
    log: list[TradeLogEntry]
    ui_config: dict = field(default_factory=dict)
    agent_memory: dict = field(default_factory=dict)
    market: str = "NSE"


@dataclass
class DailyCounters:
    day: str
    peak_open_pnl: float = 0.0
    peak_open_pnl_day: str = ""
    profit_guard_triggered_day: str = ""
    profit_ladder_day: str = ""
    profit_ladder_armed: bool = False
    profit_ladder_pullback_started: bool = False
    profit_ladder_exited_day: str = ""


@dataclass(frozen=True)
class RiskSettings:
    risk_pct: float
    sl_pct: float
    tp_pct: float
    max_trades_day: int
    max_positions: int
    max_qty_per_trade: int
    max_symbol_allocation_pct: float
    max_total_deployment_pct: float
    max_trade_invest_pct: float
    min_order_value: float
    min_price: float
    max_price: float


@dataclass(frozen=True)
class SignalSettings:
    min_buy_score: float
    min_sell_score: float
    min_short_score: float
    enable_signal_sell: bool
    enable_short_selling: bool
    max_signal_exits_per_cycle: int
    idle_buy_fallback_minutes: int


@dataclass(frozen=True)
class GuardSettings:
    enable_profit_guard: bool
    profit_guard_drawdown_pct: float
    profit_guard_after: time
    block_new_entries_on_guard: bool
    daily_profit_target: float
    reentry_cooldown_minutes: int
    reentry_min_move_pct: float
    sl_cooldown_after_stop_minutes: int
    enable_regime_entry_gate: bool
    market_regime: str


@dataclass(frozen=True)
class CycleSettings:
    risk: RiskSettings
    signals: SignalSettings
    guards: GuardSettings
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Immutable view model for the portfolio metrics block.

    Captures everything the metrics view needs in one place so the view can
    stay free of ``st.session_state`` access. ``equity_delta`` is
    ``equity - start_capital`` and ``net_realized`` is ``realized - charges``;
    both are pre-computed in the caller to keep the view formatting-only.
    """

    start_capital: float
    cash: float
    equity: float
    equity_delta: float
    unrealized: float
    realized: float
    charges: float
    net_realized: float
    today_pnl: float
    daily_profit_target: float
    currency_symbol: str

