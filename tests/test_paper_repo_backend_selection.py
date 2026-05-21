"""Phase 9 backend selection + dual-write/fallback tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from stockmarket.domain import DailyCounters, PaperState, Position, TradeLogEntry
from stockmarket.persistence import paper_repo as pr
from stockmarket.persistence.json_paper_repo import JsonPaperRepo
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
    monkeypatch.delenv("PAPER_REPO_DUAL_WRITE", raising=False)
    monkeypatch.delenv("PAPER_REPO_FALLBACK_JSON", raising=False)
    return tmp_path


def test_unset_env_returns_json_backend(isolated_cwd: Path) -> None:
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, JsonPaperRepo)


def test_env_sqlite_returns_sqlite_backend(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)


def test_env_json_explicit_returns_json_backend(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "json")
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, JsonPaperRepo)


def test_invalid_env_raises_clear_error(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "postgres")
    with pytest.raises(ValueError, match="PAPER_REPO_BACKEND"):
        get_paper_repo(isolated_cwd / "state.json", market="NSE")


def test_config_file_selects_sqlite_when_env_unset(isolated_cwd: Path) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"paper_repo_backend": "sqlite"}), encoding="utf-8"
    )
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)


def test_env_overrides_config_file(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"paper_repo_backend": "sqlite"}), encoding="utf-8"
    )
    monkeypatch.setenv("PAPER_REPO_BACKEND", "json")
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, JsonPaperRepo)


def test_invalid_config_value_raises(isolated_cwd: Path) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"paper_repo_backend": "mongo"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="paper_repo_backend"):
        get_paper_repo(isolated_cwd / "state.json", market="NSE")


def test_sqlite_db_path_resolves_from_config(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = isolated_cwd / "config"
    cfg_dir.mkdir()
    db_path = isolated_cwd / "custom" / "paper.db"
    (cfg_dir / "database_config.json").write_text(
        json.dumps({"storage": {"paper_state_database_path": str(db_path)}}), encoding="utf-8"
    )
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    repo = get_paper_repo(isolated_cwd / "state.json", market="NSE")
    assert isinstance(repo, SqlitePaperRepo)
    assert repo.path() == db_path
    assert db_path.parent.exists()


def test_dual_write_replicates_to_json_and_sqlite(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    monkeypatch.setenv("PAPER_REPO_DUAL_WRITE", "1")

    json_path = isolated_cwd / "state.json"
    repo = get_paper_repo(json_path, market="NSE")
    state, counters = _state()
    repo.save(state, counters)

    assert json_path.exists(), "dual-write should keep the JSON file current"
    json_loaded = JsonPaperRepo(json_path, market="NSE").load()
    assert json_loaded == (state, counters)

    # The primary remains SQLite and exposes its db path.
    assert repo.path().suffix == ".db"


def test_fallback_reads_json_when_sqlite_empty(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    monkeypatch.setenv("PAPER_REPO_FALLBACK_JSON", "1")

    json_path = isolated_cwd / "state.json"
    state, counters = _state()
    JsonPaperRepo(json_path, market="NSE").save(state, counters)

    repo = get_paper_repo(json_path, market="NSE")
    loaded = repo.load()
    assert loaded is not None
    assert loaded == (state, counters)


def test_fallback_skipped_when_sqlite_already_has_state(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    monkeypatch.setenv("PAPER_REPO_FALLBACK_JSON", "1")
    monkeypatch.setenv("PAPER_REPO_DUAL_WRITE", "1")

    json_path = isolated_cwd / "state.json"
    repo = get_paper_repo(json_path, market="NSE")
    state, counters = _state()
    repo.save(state, counters)

    # Mutate JSON on disk; loader must prefer SQLite because primary is populated.
    stale_payload = json.loads(json_path.read_text(encoding="utf-8"))
    stale_payload["cash"] = -1.0
    json_path.write_text(json.dumps(stale_payload), encoding="utf-8")

    loaded = get_paper_repo(json_path, market="NSE").load()
    assert loaded is not None
    assert loaded[0].cash == state.cash


def test_fallback_disabled_returns_none_on_miss(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_REPO_BACKEND", "sqlite")
    # No fallback / dual-write flags.

    json_path = isolated_cwd / "state.json"
    state, counters = _state()
    JsonPaperRepo(json_path, market="NSE").save(state, counters)

    repo = get_paper_repo(json_path, market="NSE")
    assert repo.load() is None  # SQLite empty and fallback disabled
