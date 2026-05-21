from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import pytest

from stockmarket.cycle.adapters.dashboard_signals import StaticSignalSource
from stockmarket.cycle.adapters.log_history import LogHistoryQuery
from stockmarket.cycle.adapters.session_prices import NoOpPriceRefresh
from stockmarket.cycle.adapters.streamlit_broker import StreamlitBroker
from stockmarket.cycle.context import CycleContext
from stockmarket.cycle.ports import RankedSignals
from stockmarket.cycle.services import Services
from stockmarket.cycle.steps.exits import (
    evaluate_profit_ladder,
    force_exits_long,
    intraday_square_off,
)
from stockmarket.cycle.steps.gates import block_on_weekend, gate_daily_trade_cap
from stockmarket.domain import (
    CycleSettings,
    DailyCounters,
    GuardSettings,
    PaperState,
    Position,
    RiskSettings,
    SignalSettings,
    TradeLogEntry,
)


class FixedClock:
    def __init__(self, now: datetime, *, square_off: time = time(15, 15)):
        self._now = now
        self._square_off = square_off

    def now(self) -> datetime:
        return self._now

    def in_entry_window(self, now: datetime) -> bool:
        return True

    def square_off_time(self) -> time:
        return self._square_off

    def market_open_time(self) -> time:
        return time(9, 15)


class MemoryRepo:
    def load(self):
        return None

    def save(self, state, counters):
        self.state = state
        self.counters = counters

    def path(self):
        from pathlib import Path

        return Path("memory")


def _settings(**overrides) -> CycleSettings:
    risk = RiskSettings(
        risk_pct=1.0,
        sl_pct=0.01,
        tp_pct=0.02,
        max_trades_day=3,
        max_positions=5,
        max_qty_per_trade=10,
        max_symbol_allocation_pct=20.0,
        max_total_deployment_pct=50.0,
        max_trade_invest_pct=10.0,
        min_order_value=1000.0,
        min_price=10.0,
        max_price=5000.0,
    )
    signals = SignalSettings(
        min_buy_score=60.0,
        min_sell_score=60.0,
        min_short_score=60.0,
        enable_signal_sell=True,
        enable_short_selling=True,
        max_signal_exits_per_cycle=2,
        idle_buy_fallback_minutes=30,
    )
    guards = GuardSettings(
        enable_profit_guard=True,
        profit_guard_drawdown_pct=30.0,
        profit_guard_after=time(12, 0),
        block_new_entries_on_guard=True,
        daily_profit_target=5000.0,
        reentry_cooldown_minutes=15,
        reentry_min_move_pct=0.5,
        sl_cooldown_after_stop_minutes=10,
        enable_regime_entry_gate=False,
        market_regime="unknown",
    )
    return CycleSettings(risk=risk, signals=signals, guards=guards, symbols=("AAA",))


def _ctx(
    *,
    now: datetime,
    state: PaperState | None = None,
    counters: DailyCounters | None = None,
    signals: RankedSignals | None = None,
) -> CycleContext:
    today = now.strftime("%Y-%m-%d")
    return CycleContext(
        now=now,
        today=today,
        settings=_settings(),
        state=state
        or PaperState(
            start_capital=100000.0,
            cash=100000.0,
            realized=0.0,
            charges=0.0,
            holdings={},
            shorts={},
            prices={},
            log=[],
        ),
        counters=counters or DailyCounters(day=today),
        signals=signals or RankedSignals(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()),
        entries_today=0,
        open_positions=0,
    )


def _svc(now: datetime) -> Services:
    repo = MemoryRepo()
    return Services(
        clock=FixedClock(now),
        broker=StreamlitBroker(lambda side, val: 1.0),
        signals=StaticSignalSource(),
        history=LogHistoryQuery(time(9, 15)),
        repo=repo,
        prices=NoOpPriceRefresh(),
        charges_fn=lambda side, val: 1.0,
    )


def test_block_on_weekend_halts_cycle():
    saturday = datetime(2026, 5, 16, 10, 0, 0)
    ctx = _ctx(now=saturday)
    ctx = block_on_weekend(ctx, _svc(saturday))
    assert ctx.halt_cycle is True
    assert "weekend" in (ctx.halt_reason or "").lower()


def test_force_exits_long_sl():
    now = datetime(2026, 5, 18, 10, 0, 0)
    state = PaperState(
        start_capital=100000.0,
        cash=90000.0,
        realized=0.0,
        charges=0.0,
        holdings={"AAA": Position(qty=5, avg=100.0, stop=99.0, target=102.0)},
        shorts={},
        prices={"AAA": 98.0},
        log=[],
    )
    ctx = _ctx(now=now, state=state)
    ctx = force_exits_long(ctx, _svc(now))
    assert any("SL hit" in action for action in ctx.actions)
    assert "AAA" not in ctx.state.holdings


def test_profit_ladder_arms_and_exits():
    now = datetime(2026, 5, 18, 11, 0, 0)
    settings = _settings()
    state = PaperState(
        start_capital=100000.0,
        cash=50000.0,
        realized=0.0,
        charges=0.0,
        holdings={"AAA": Position(qty=2, avg=100.0, stop=99.0, target=102.0)},
        shorts={},
        prices={"AAA": 110.0},
        log=[
            TradeLogEntry(
                ts="2026-05-18 09:00:00",
                symbol="AAA",
                side="SELL",
                qty=1,
                price=100.0,
                charges=1.0,
                realized_delta=7000.0,
                reason="seed",
                cash_after=50000.0,
            )
        ],
    )
    ctx = CycleContext(
        now=now,
        today=now.strftime("%Y-%m-%d"),
        settings=settings,
        state=state,
        counters=DailyCounters(day=now.strftime("%Y-%m-%d")),
        signals=RankedSignals(pd.DataFrame([{"symbol": "AAA"}]), pd.DataFrame(), pd.DataFrame()),
    )
    ctx = evaluate_profit_ladder(ctx, _svc(now))
    assert ctx.halt_cycle is True
    assert "AAA" not in ctx.state.holdings


def test_intraday_square_off_halts():
    now = datetime(2026, 5, 18, 15, 20, 0)
    state = PaperState(
        start_capital=100000.0,
        cash=90000.0,
        realized=0.0,
        charges=0.0,
        holdings={"AAA": Position(qty=1, avg=100.0, stop=99.0, target=102.0)},
        shorts={},
        prices={"AAA": 101.0},
        log=[],
    )
    ctx = _ctx(now=now, state=state)
    ctx = intraday_square_off(ctx, _svc(now))
    assert ctx.halt_cycle is True
    assert "AAA" not in ctx.state.holdings


def test_gate_daily_trade_cap():
    now = datetime(2026, 5, 18, 10, 0, 0)
    ctx = _ctx(now=now)
    ctx.entries_today = 3
    ctx = gate_daily_trade_cap(ctx, _svc(now))
    assert ctx.halt_cycle is True
    assert any("max 3 trades" in action for action in ctx.actions)
