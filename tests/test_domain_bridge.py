from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockmarket.domain import DailyCounters, PaperState, Position, TradeLogEntry
from stockmarket.state.session_bridge import (
    counters_to_session_state,
    paper_state_to_session_state,
    session_state_to_counters,
    session_state_to_paper_state,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "simple_paper_state_minimal.json"


def _session_from_payload(payload: dict) -> dict:
    return {
        "s_cash": payload["cash"],
        "s_start": payload["start"],
        "s_realized": payload["realized"],
        "s_charges": payload["charges"],
        "s_holdings": payload["holdings"],
        "s_shorts": payload["shorts"],
        "s_ui_config": payload["ui_config"],
        "s_log": payload["log"],
        "s_prices": payload["prices"],
        "s_agent_memory": payload["agent_memory"],
        "s_peak_open_pnl": payload["peak_open_pnl"],
        "s_peak_open_pnl_day": payload["peak_open_pnl_day"],
        "s_profit_guard_triggered_day": payload["profit_guard_triggered_day"],
        "s_profit_ladder_day": payload["profit_ladder_day"],
        "s_profit_ladder_armed": payload["profit_ladder_armed"],
        "s_profit_ladder_pullback_started": payload["profit_ladder_pullback_started"],
        "s_profit_ladder_exited_day": payload["profit_ladder_exited_day"],
        "selected_market": payload["market"],
    }


def test_bridge_round_trip_preserves_paper_state_and_counters():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    session = _session_from_payload(payload)

    state = session_state_to_paper_state(session)
    counters = session_state_to_counters(session)
    out: dict = {}
    paper_state_to_session_state(out, state)
    counters_to_session_state(out, counters)

    assert out["s_cash"] == pytest.approx(payload["cash"], abs=1e-6)
    assert out["s_start"] == pytest.approx(payload["start"], abs=1e-6)
    assert out["s_realized"] == pytest.approx(payload["realized"], abs=1e-6)
    assert out["s_charges"] == pytest.approx(payload["charges"], abs=1e-6)
    assert out["s_holdings"].keys() == payload["holdings"].keys()
    assert out["s_shorts"].keys() == payload["shorts"].keys()
    assert len(out["s_log"]) == len(payload["log"])
    assert out["s_peak_open_pnl"] == pytest.approx(payload["peak_open_pnl"], abs=1e-6)
    assert out["s_peak_open_pnl_day"] == payload["peak_open_pnl_day"]
    assert out["s_profit_guard_triggered_day"] == payload["profit_guard_triggered_day"]
    assert out["s_profit_ladder_day"] == payload["profit_ladder_day"]
    assert out["s_profit_ladder_armed"] is payload["profit_ladder_armed"]
    assert out["s_profit_ladder_pullback_started"] is payload["profit_ladder_pullback_started"]
    assert out["s_profit_ladder_exited_day"] == payload["profit_ladder_exited_day"]
    assert out["selected_market"] == payload["market"]


def test_bridge_defaults_legacy_position_stops_and_targets():
    session = {
        "s_start": 100000.0,
        "s_cash": 99000.0,
        "s_holdings": {"LONG": {"qty": 10, "avg": 100.0}},
        "s_shorts": {"SHORT": {"qty": 5, "avg": 200.0}},
        "s_ui_config": {"sl_pct_display": 1.0, "tp_pct_display": 2.0},
    }

    state = session_state_to_paper_state(session)

    assert state.holdings["LONG"] == Position(qty=10, avg=100.0, stop=99.0, target=102.0)
    assert state.shorts["SHORT"] == Position(qty=5, avg=200.0, stop=202.0, target=196.0)


def test_paper_state_writes_dict_compatible_log_rows():
    state = PaperState(
        start_capital=100000.0,
        cash=99900.0,
        realized=0.0,
        charges=1.0,
        holdings={"ABC": Position(qty=1, avg=100.0, stop=99.2, target=101.6)},
        shorts={},
        prices={"ABC": 100.0},
        log=[
            TradeLogEntry(
                ts="2026-01-01 09:30:00",
                symbol="ABC",
                side="BUY",
                qty=1,
                price=100.0,
                charges=1.0,
                realized_delta=0.0,
                reason="test",
                cash_after=99900.0,
            )
        ],
        market="NSE",
    )

    session: dict = {}
    paper_state_to_session_state(session, state)

    assert session["s_log"] == [
        {
            "ts": "2026-01-01 09:30:00",
            "symbol": "ABC",
            "side": "BUY",
            "qty": 1,
            "price": 100.0,
            "charges": 1.0,
            "realized_delta": 0.0,
            "reason": "test",
            "cash_after": 99900.0,
            "tradebookid": 0,
        }
    ]


def test_session_log_without_tradebookid_defaults_to_zero():
    session = {
        "s_start": 100000.0,
        "s_cash": 99900.0,
        "s_log": [
            {
                "ts": "2026-01-01 09:30:00",
                "symbol": "ABC",
                "side": "BUY",
                "qty": 1,
                "price": 100.0,
                "charges": 1.0,
                "realized_delta": 0.0,
                "reason": "legacy row",
                "cash_after": 99900.0,
            }
        ],
    }

    state = session_state_to_paper_state(session)

    assert state.log[0].tradebookid == 0


def test_counters_write_day_when_peak_day_is_empty():
    session: dict = {}
    counters_to_session_state(session, DailyCounters(day="2026-01-01"))

    assert session["s_peak_open_pnl_day"] == "2026-01-01"

