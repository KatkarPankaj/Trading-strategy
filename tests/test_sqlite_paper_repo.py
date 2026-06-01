"""Unit tests for :class:`SqlitePaperRepo`."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from stockmarket.domain import DailyCounters, PaperState, Position, TradeLogEntry
from stockmarket.persistence.sqlite_paper_repo import SqlitePaperRepo


def _rich_state(market: str = "NSE") -> tuple[PaperState, DailyCounters]:
    holdings = {
        "RELIANCE": Position(qty=10, avg=2500.0, stop=2450.0, target=2600.0),
        "INFY": Position(qty=5, avg=1500.0, stop=1470.0, target=1560.0),
    }
    shorts = {"TCS": Position(qty=2, avg=3500.0, stop=3535.0, target=3430.0)}
    log = [
        TradeLogEntry(
            ts="2026-05-20T10:15:00",
            symbol="RELIANCE",
            side="BUY",
            qty=10,
            price=2500.0,
            charges=12.5,
            realized_delta=0.0,
            reason="entry",
            cash_after=75000.0,
        ),
        TradeLogEntry(
            ts="2026-05-20T11:00:00",
            symbol="INFY",
            side="BUY",
            qty=5,
            price=1500.0,
            charges=7.5,
            realized_delta=0.0,
            reason="entry",
            cash_after=67500.0,
        ),
        TradeLogEntry(
            ts="2026-05-20T14:30:00",
            symbol="TCS",
            side="SHORT",
            qty=2,
            price=3500.0,
            charges=7.0,
            realized_delta=0.0,
            reason="short entry",
            cash_after=67500.0,
        ),
    ]
    state = PaperState(
        start_capital=100_000.0,
        cash=67_500.0,
        realized=125.0,
        charges=27.0,
        holdings=holdings,
        shorts=shorts,
        prices={"RELIANCE": 2510.0, "INFY": 1505.0, "TCS": 3490.0},
        log=log,
        ui_config={"sl_pct": 0.008, "tp_pct": 0.016, "feature_flags": ["alpha", "beta"]},
        agent_memory={
            "version": 3,
            "weights": {f"symbol_{i}": i * 0.001 for i in range(200)},
            "notes": "x" * 5_000,
        },
        market=market,
    )
    counters = DailyCounters(
        day="2026-05-20",
        peak_open_pnl=320.0,
        peak_open_pnl_day="2026-05-20",
        profit_guard_triggered_day="2026-05-20",
        profit_ladder_day="2026-05-20",
        profit_ladder_armed=True,
        profit_ladder_pullback_started=False,
        profit_ladder_exited_day="",
    )
    return state, counters


def test_load_on_empty_db_returns_none(tmp_path: Path) -> None:
    repo = SqlitePaperRepo(tmp_path / "p.db", market="NSE")
    assert repo.load() is None
    assert repo.path() == tmp_path / "p.db"


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    state, counters = _rich_state()
    repo = SqlitePaperRepo(tmp_path / "p.db", market="NSE")
    repo.save(state, counters)
    loaded = repo.load()
    assert loaded is not None
    loaded_state, loaded_counters = loaded
    assert loaded_state == state
    assert loaded_counters == counters
    assert repo.updated_at()


def test_clear_market_removes_only_selected_market(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    nse_state, nse_counters = _rich_state("NSE")
    us_state, us_counters = _rich_state("US")
    SqlitePaperRepo(db, market="NSE").save(nse_state, nse_counters)
    SqlitePaperRepo(db, market="US").save(us_state, us_counters)

    SqlitePaperRepo(db, market="NSE").clear_market()

    assert SqlitePaperRepo(db, market="NSE").load() is None
    us_loaded = SqlitePaperRepo(db, market="US").load()
    assert us_loaded is not None
    assert us_loaded[0].market == "US"


def test_save_is_idempotent_and_replaces_state(tmp_path: Path) -> None:
    state, counters = _rich_state()
    repo = SqlitePaperRepo(tmp_path / "p.db", market="NSE")
    repo.save(state, counters)
    repo.save(state, counters)

    smaller = replace(
        state,
        holdings={"RELIANCE": state.holdings["RELIANCE"]},
        shorts={},
        prices={"RELIANCE": 2515.0},
        log=state.log[:1],
    )
    repo.save(smaller, counters)
    loaded = repo.load()
    assert loaded is not None
    loaded_state, _ = loaded
    assert list(loaded_state.holdings) == ["RELIANCE"]
    assert loaded_state.shorts == {}
    assert loaded_state.prices == {"RELIANCE": 2515.0}
    assert len(loaded_state.log) == 1


def test_multi_market_isolation_in_one_db(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    nse_state, nse_counters = _rich_state("NSE")
    us_state, us_counters = _rich_state("US")
    us_state = replace(us_state, cash=42_000.0, prices={"AAPL": 200.0})

    SqlitePaperRepo(db, market="NSE").save(nse_state, nse_counters)
    SqlitePaperRepo(db, market="US").save(us_state, us_counters)

    nse_loaded = SqlitePaperRepo(db, market="NSE").load()
    us_loaded = SqlitePaperRepo(db, market="US").load()
    assert nse_loaded is not None and us_loaded is not None
    assert nse_loaded[0].cash == 67_500.0
    assert us_loaded[0].cash == 42_000.0
    assert us_loaded[0].prices == {"AAPL": 200.0}


def test_reopen_after_close_preserves_state(tmp_path: Path) -> None:
    state, counters = _rich_state()
    db = tmp_path / "p.db"
    SqlitePaperRepo(db, market="NSE").save(state, counters)
    # Re-instantiate (simulates a process restart / new dashboard session).
    loaded = SqlitePaperRepo(db, market="NSE").load()
    assert loaded is not None
    assert loaded[0] == state


def test_agent_memory_stored_as_json_text_not_blob(tmp_path: Path) -> None:
    state, counters = _rich_state()
    db = tmp_path / "p.db"
    SqlitePaperRepo(db, market="NSE").save(state, counters)

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT typeof(agent_memory), typeof(ui_config), agent_memory"
            " FROM paper_state WHERE market = ?",
            ("NSE",),
        ).fetchone()
    assert row[0] == "text"
    assert row[1] == "text"
    # Round-trips through SQLite load to confirm it's serialized text.
    loaded = SqlitePaperRepo(db, market="NSE").load()
    assert loaded is not None
    decoded = loaded[0].agent_memory
    assert decoded["version"] == 3


def test_concurrent_readers_see_committed_state(tmp_path: Path) -> None:
    state, counters = _rich_state()
    db = tmp_path / "p.db"
    writer = SqlitePaperRepo(db, market="NSE")
    writer.save(state, counters)

    reader_a = SqlitePaperRepo(db, market="NSE").load()
    reader_b = SqlitePaperRepo(db, market="NSE").load()
    assert reader_a == reader_b
    assert reader_a is not None and reader_a[0].cash == state.cash


def test_tradebookid_defaults_to_zero_round_trip(tmp_path: Path) -> None:
    state, counters = _rich_state()
    repo = SqlitePaperRepo(tmp_path / "p.db", market="NSE")
    repo.save(state, counters)
    loaded = repo.load()
    assert loaded is not None
    for entry in loaded[0].log:
        assert entry.tradebookid == 0


def test_tradebookid_explicit_value_round_trips(tmp_path: Path) -> None:
    state, counters = _rich_state()
    state = replace(
        state,
        log=[
            replace(state.log[0], tradebookid=42),
            replace(state.log[1], tradebookid=7),
            replace(state.log[2], tradebookid=0),
        ],
    )
    repo = SqlitePaperRepo(tmp_path / "p.db", market="NSE")
    repo.save(state, counters)
    loaded = repo.load()
    assert loaded is not None
    assert [e.tradebookid for e in loaded[0].log] == [42, 7, 0]


def test_trade_log_schema_has_tradebookid_column(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    SqlitePaperRepo(db, market="NSE")  # ensures schema
    with sqlite3.connect(db) as conn:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(trade_log)")}
    assert "tradebookid" in cols
    # PRAGMA columns: cid, name, type, notnull, dflt_value, pk
    assert cols["tradebookid"][2].upper() == "INTEGER"
    assert int(cols["tradebookid"][4]) == 0


def test_alter_table_adds_tradebookid_to_pre_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    legacy_ddl = """
        CREATE TABLE trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            charges REAL NOT NULL,
            realized_delta REAL NOT NULL,
            reason TEXT NOT NULL,
            cash_after REAL NOT NULL
        )
    """
    with sqlite3.connect(db) as conn:
        conn.execute(legacy_ddl)
        conn.commit()
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    assert "tradebookid" not in cols_before

    state, counters = _rich_state()
    state = replace(state, log=[replace(state.log[0], tradebookid=99)])
    repo = SqlitePaperRepo(db, market="NSE")
    with sqlite3.connect(db) as conn:
        cols_after = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    assert "tradebookid" in cols_after

    repo.save(state, counters)
    loaded = repo.load()
    assert loaded is not None
    assert loaded[0].log[0].tradebookid == 99
