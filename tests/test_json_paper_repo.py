from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stockmarket.persistence.json_paper_repo import JsonPaperRepo
from stockmarket.state import session_state_to_counters, session_state_to_paper_state


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "simple_paper_state_minimal.json"


class SessionState(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def _session_from_payload(payload: dict) -> SessionState:
    return SessionState(
        s_cash=payload["cash"],
        s_start=payload["start"],
        s_realized=payload["realized"],
        s_charges=payload["charges"],
        s_holdings=payload["holdings"],
        s_shorts=payload["shorts"],
        s_ui_config=payload["ui_config"],
        s_log=payload["log"],
        s_prices=payload["prices"],
        s_agent_memory=payload["agent_memory"],
        s_peak_open_pnl=payload["peak_open_pnl"],
        s_peak_open_pnl_day=payload["peak_open_pnl_day"],
        s_profit_guard_triggered_day=payload["profit_guard_triggered_day"],
        s_profit_ladder_day=payload["profit_ladder_day"],
        s_profit_ladder_armed=payload["profit_ladder_armed"],
        s_profit_ladder_pullback_started=payload["profit_ladder_pullback_started"],
        s_profit_ladder_exited_day=payload["profit_ladder_exited_day"],
        selected_market=payload["market"],
    )


def test_json_paper_repo_round_trips_minimal_payload(tmp_path):
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    source = tmp_path / "simple_paper_state.json"
    target = tmp_path / "round_trip.json"
    source.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    loaded = JsonPaperRepo(source, market="NSE").load()
    assert loaded is not None

    JsonPaperRepo(target, market="NSE").save(*loaded)
    saved = json.loads(target.read_text(encoding="utf-8"))

    assert list(saved.keys()) == list(payload.keys())
    assert saved == payload


def test_json_paper_repo_matches_legacy_save_payload(tmp_path):
    import dashboard_simple

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    session = _session_from_payload(payload)
    legacy_file = tmp_path / "legacy.json"
    repo_file = tmp_path / "repo.json"

    with (
        patch.object(dashboard_simple, "st", SimpleNamespace(session_state=session)),
        patch.object(dashboard_simple, "_state_file", return_value=legacy_file),
    ):
        dashboard_simple._save_state()

    state = session_state_to_paper_state(session)
    counters = session_state_to_counters(session)
    JsonPaperRepo(repo_file, market=payload["market"]).save(state, counters)

    legacy_payload = json.loads(legacy_file.read_text(encoding="utf-8"))
    repo_payload = json.loads(repo_file.read_text(encoding="utf-8"))
    assert repo_payload == legacy_payload


def test_json_paper_repo_round_trips_tradebookid(tmp_path):
    from dataclasses import replace

    from stockmarket.domain import TradeLogEntry

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    source = tmp_path / "in.json"
    target = tmp_path / "out.json"
    source.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    loaded = JsonPaperRepo(source, market="NSE").load()
    assert loaded is not None
    state, counters = loaded
    state = replace(
        state,
        log=[
            TradeLogEntry(
                ts="2026-01-01 09:30:00",
                symbol="ABC",
                side="BUY",
                qty=1,
                price=100.0,
                charges=1.0,
                realized_delta=0.0,
                reason="entry",
                cash_after=99900.0,
                tradebookid=4321,
            )
        ],
    )
    JsonPaperRepo(target, market="NSE").save(state, counters)

    reloaded = JsonPaperRepo(target, market="NSE").load()
    assert reloaded is not None
    assert reloaded[0].log[0].tradebookid == 4321


def test_dashboard_uses_json_paper_repo_when_flag_enabled(tmp_path, monkeypatch):
    import dashboard_simple

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    state_file = tmp_path / "simple_paper_state.json"
    monkeypatch.setenv("USE_PAPER_REPO", "1")

    with (
        patch.object(dashboard_simple, "st", SimpleNamespace(session_state=_session_from_payload(payload))),
        patch.object(dashboard_simple, "_state_file", return_value=state_file),
    ):
        dashboard_simple._save_state()

    fresh_session = SessionState(selected_market="NSE")
    with (
        patch.object(dashboard_simple, "st", SimpleNamespace(session_state=fresh_session)),
        patch.object(dashboard_simple, "_state_file", return_value=state_file),
    ):
        dashboard_simple._init_state(starting_capital=123.0)

    assert fresh_session.s_cash == payload["cash"]
    assert fresh_session.s_start == payload["start"]
    assert fresh_session.s_state_file == str(state_file)
    assert fresh_session.selected_market == payload["market"]
