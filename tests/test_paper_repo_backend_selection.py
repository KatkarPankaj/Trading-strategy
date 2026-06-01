"""SQLite-only paper repo factory tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockmarket.domain import DailyCounters, PaperState, Position, TradeLogEntry
from stockmarket.persistence.paper_repo import get_paper_repo
from stockmarket.persistence.sqlite_paper_repo import SqlitePaperRepo


def _state(market: str = "NSE") -> tuple[PaperState, DailyCounters]:
    return (
        PaperState(
            start_capital=10_000.0,
            cash=9_500.0,
            realized=12.5,
            charges=3.5,
            holdings={"AAPL": Position(qty=1, avg=200.0, stop=198.0, target=204.0)},
            shorts={},
            prices={"AAPL": 201.0},
            log=[
                TradeLogEntry(
                    ts="2026-05-21T10:00:00",
                    symbol="AAPL",
                    side="BUY",
                    qty=1,
                    price=200.0,
                    charges=1.0,
                    realized_delta=0.0,
                    reason="entry",
                    cash_after=9_799.0,
                )
            ],
            ui_config={"sl_pct": 0.01},
            agent_memory={"k": "v"},
            market=market,
        ),
        DailyCounters(day="2026-05-21", peak_open_pnl=2.5, peak_open_pnl_day="2026-05-21"),
    )


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PAPER_REPO_BACKEND", raising=False)
    return tmp_path


def test_factory_always_returns_sqlite_backend(isolated_cwd: Path) -> None:
    repo = get_paper_repo(isolated_cwd / "ignored_state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)
    assert repo.path() == Path(".database") / "paper_state.db"


def test_paper_repo_backend_env_is_ignored(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "json")
    repo = get_paper_repo(isolated_cwd / "ignored_state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)


def test_legacy_config_backend_value_is_ignored(isolated_cwd: Path) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"paper_repo_backend": "json"}), encoding="utf-8"
    )
    repo = get_paper_repo(isolated_cwd / "ignored_state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)


def test_sqlite_db_path_resolves_from_config(isolated_cwd: Path) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    db_path = isolated_cwd / "custom" / "paper.db"
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"storage": {"paper_state_database_path": str(db_path)}}), encoding="utf-8"
    )
    repo = get_paper_repo(isolated_cwd / "ignored_state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)
    assert repo.path() == db_path
    assert db_path.parent.exists()


def test_factory_ignores_legacy_json_path(isolated_cwd: Path) -> None:
    json_path = isolated_cwd / "state.json"
    json_path.write_text(json.dumps({"log": [{"symbol": "AAPL"}]}), encoding="utf-8")

    repo = get_paper_repo(json_path, market="NSE")
    assert isinstance(repo, SqlitePaperRepo)
    assert repo.load() is None


def test_factory_sqlite_round_trip(isolated_cwd: Path) -> None:
    state, counters = _state()
    repo = get_paper_repo(isolated_cwd / "ignored_state.json", market="NSE")

    repo.save(state, counters)
    loaded = repo.load()

    assert loaded == (state, counters)
