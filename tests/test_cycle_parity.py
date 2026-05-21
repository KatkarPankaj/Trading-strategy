from __future__ import annotations

import json
import sys
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

FIXTURE = Path(__file__).parent / "fixtures" / "simple_paper_state_minimal.json"


class SessionState(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


def _session_from_payload(payload: dict) -> SessionState:
    return SessionState(
        s_cash=payload["cash"],
        s_start=payload["start"],
        s_realized=payload["realized"],
        s_charges=payload["charges"],
        s_holdings=dict(payload["holdings"]),
        s_shorts=dict(payload["shorts"]),
        s_ui_config=dict(payload.get("ui_config", {})),
        s_log=list(payload.get("log", [])),
        s_prices=dict(payload.get("prices", {})),
        s_agent_memory=dict(payload.get("agent_memory", {})),
        s_peak_open_pnl=payload.get("peak_open_pnl", 0.0),
        s_peak_open_pnl_day=payload.get("peak_open_pnl_day", ""),
        s_profit_guard_triggered_day=payload.get("profit_guard_triggered_day", ""),
        s_profit_ladder_day=payload.get("profit_ladder_day", ""),
        s_profit_ladder_armed=payload.get("profit_ladder_armed", False),
        s_profit_ladder_pullback_started=payload.get("profit_ladder_pullback_started", False),
        s_profit_ladder_exited_day=payload.get("profit_ladder_exited_day", ""),
        selected_market=payload.get("market", "NSE"),
    )


def _cycle_kwargs():
    return dict(
        risk_pct=1.0,
        max_trades_day=5,
        max_positions=5,
        sl_pct=0.008,
        tp_pct=0.016,
        min_buy_score=60.0,
        enable_signal_sell=False,
        min_sell_score=60.0,
        max_signal_exits_per_cycle=1,
        enable_short_selling=False,
        min_short_score=60.0,
        enable_profit_guard=False,
        profit_guard_drawdown_pct=30.0,
        profit_guard_after=time(12, 0),
        block_new_entries_on_guard=True,
        daily_profit_target=0.0,
        reentry_cooldown_minutes=0,
        reentry_min_move_pct=0.0,
        enable_regime_entry_gate=False,
        market_regime="unknown",
        sl_cooldown_after_stop_minutes=0,
        max_qty_per_trade=10,
        symbols=["AAA"],
        min_price=10.0,
        max_price=5000.0,
        max_symbol_allocation_pct=20.0,
        max_total_deployment_pct=50.0,
        min_order_value=1000.0,
        idle_buy_fallback_minutes=30,
        max_trade_invest_pct=10.0,
    )


def _run_both(session: SessionState, now: datetime, buy_df, sell_df, sell_exit_df, monkeypatch):
    import dashboard_simple

    empty = pd.DataFrame()
    fake_st = SimpleNamespace(session_state=session)
    kwargs = _cycle_kwargs()

    with (
        patch.object(dashboard_simple, "st", fake_st),
        patch.object(dashboard_simple, "_save_state"),
        patch.object(dashboard_simple, "_refresh_holding_prices"),
        patch.object(dashboard_simple, "market_now", return_value=now),
    ):
        legacy_session = SessionState(dict(session))
        legacy_actions = dashboard_simple._auto_paper_cycle(
            buy_df, sell_df, sell_exit_df, **kwargs
        )
        legacy_snapshot = {
            "cash": legacy_session.s_cash,
            "holdings": dict(legacy_session.s_holdings),
            "shorts": dict(legacy_session.s_shorts),
            "log": list(legacy_session.s_log),
        }

        pipeline_session = SessionState(dict(session))
        monkeypatch.setenv("USE_TRADING_CYCLE", "1")
        pipeline_actions = dashboard_simple._auto_paper_cycle(
            buy_df, sell_df, sell_exit_df, **kwargs
        )
        pipeline_snapshot = {
            "cash": pipeline_session.s_cash,
            "holdings": dict(pipeline_session.s_holdings),
            "shorts": dict(pipeline_session.s_shorts),
            "log": list(pipeline_session.s_log),
        }

    return legacy_actions, pipeline_actions, legacy_snapshot, pipeline_snapshot


def test_parity_weekend_block(monkeypatch):
    import dashboard_simple

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = _session_from_payload(payload)
    saturday = datetime(2026, 5, 16, 10, 0, 0)
    signal = pd.DataFrame([{"symbol": "AAA", "price": 100.0, "buy_score": 80.0}])

    legacy, pipeline, _, _ = _run_both(
        session, saturday, signal, pd.DataFrame(), pd.DataFrame(), monkeypatch
    )
    assert legacy == pipeline
    assert legacy == ["Market closed (weekend) — no automated actions"]


def test_parity_empty_signals_refresh_only(monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = _session_from_payload(payload)
    weekday = datetime(2026, 5, 18, 10, 0, 0)
    empty = pd.DataFrame()

    legacy, pipeline, legacy_snap, pipeline_snap = _run_both(
        session, weekday, empty, empty, empty, monkeypatch
    )
    assert legacy == pipeline == []
    assert legacy_snap == pipeline_snap


def test_parity_max_trades_gate(monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    today = "2026-05-18"
    payload["log"] = [
        {
            "ts": f"{today} 09:30:00",
            "symbol": "AAA",
            "side": "BUY",
            "qty": 1,
            "price": 100.0,
            "charges": 1.0,
            "realized_delta": 0.0,
            "reason": "test",
            "cash_after": 99000.0,
        }
        for _ in range(5)
    ]
    session = _session_from_payload(payload)
    weekday = datetime(2026, 5, 18, 11, 0, 0)
    signal = pd.DataFrame(
        [{"symbol": "BBB", "price": 100.0, "buy_score": 90.0, "buy_signal": "READY"}]
    )

    import dashboard_simple

    fake_st = SimpleNamespace(session_state=session)
    kwargs = _cycle_kwargs()
    kwargs["max_trades_day"] = 5

    with (
        patch.object(dashboard_simple, "st", fake_st),
        patch.object(dashboard_simple, "_save_state"),
        patch.object(dashboard_simple, "_refresh_holding_prices"),
        patch.object(dashboard_simple, "market_now", return_value=weekday),
        patch.object(dashboard_simple, "_in_entry_window", return_value=True),
        patch.object(
            dashboard_simple,
            "_rank_signals",
            return_value=(signal, pd.DataFrame(), pd.DataFrame(), []),
        ),
        patch.object(
            dashboard_simple,
            "_apply_effective_scores",
            side_effect=lambda buy, sell, exit, bias: (buy, sell, exit),
        ),
    ):
        legacy = dashboard_simple._auto_paper_cycle(
            signal, pd.DataFrame(), pd.DataFrame(), **kwargs
        )
        monkeypatch.setenv("USE_TRADING_CYCLE", "1")
        pipeline = dashboard_simple._auto_paper_cycle(
            signal, pd.DataFrame(), pd.DataFrame(), **kwargs
        )

    assert legacy == pipeline
    assert any("max 5 trades" in action for action in legacy)
