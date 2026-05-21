"""Tests for ``scripts/migrate_paper_state_json_to_sqlite.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from stockmarket.persistence.json_paper_repo import JsonPaperRepo
from stockmarket.persistence.sqlite_paper_repo import SqlitePaperRepo

FIXTURE = Path(__file__).parent / "fixtures" / "simple_paper_state_minimal.json"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_paper_state_json_to_sqlite.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("migrate_paper_state_json_to_sqlite", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_payload(tmp_path: Path) -> Path:
    src = tmp_path / "simple_paper_state.json"
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["cash"] = 91_234.5
    payload["log"] = [
        {
            "ts": "2026-05-18T10:00:00",
            "symbol": "RELIANCE",
            "side": "BUY",
            "qty": 1,
            "price": 2400.0,
            "charges": 1.2,
            "realized_delta": 0.0,
            "reason": "entry",
            "cash_after": 97_600.0,
        }
    ]
    payload["holdings"] = {
        "RELIANCE": {"qty": 1, "avg": 2400.0, "stop": 2380.0, "target": 2440.0}
    }
    payload["prices"] = {"RELIANCE": 2410.0}
    payload["agent_memory"] = {"k": "v"}
    src.write_text(json.dumps(payload), encoding="utf-8")
    return src


def test_migrate_matches_fresh_sqlite_save(fixture_payload: Path, tmp_path: Path) -> None:
    script = _load_script_module()
    db_via_script = tmp_path / "via_script.db"
    db_direct = tmp_path / "direct.db"

    summary = script.migrate(fixture_payload, "NSE", db_via_script)
    assert summary.holdings == 1
    assert summary.log_rows == 1
    assert summary.prices == 1

    loaded_json = JsonPaperRepo(fixture_payload, market="NSE").load()
    assert loaded_json is not None
    SqlitePaperRepo(db_direct, market="NSE").save(*loaded_json)

    via_script = SqlitePaperRepo(db_via_script, market="NSE").load()
    direct = SqlitePaperRepo(db_direct, market="NSE").load()
    assert via_script == direct
    assert via_script == loaded_json


def test_migrate_is_idempotent(fixture_payload: Path, tmp_path: Path) -> None:
    script = _load_script_module()
    db = tmp_path / "p.db"

    script.migrate(fixture_payload, "NSE", db)
    first = SqlitePaperRepo(db, market="NSE").load()
    script.migrate(fixture_payload, "NSE", db)
    second = SqlitePaperRepo(db, market="NSE").load()

    assert first == second
    assert fixture_payload.exists(), "migration must not destroy source JSON"


def test_migrate_preserves_source_json(fixture_payload: Path, tmp_path: Path) -> None:
    script = _load_script_module()
    before = fixture_payload.read_text(encoding="utf-8")
    script.migrate(fixture_payload, "NSE", tmp_path / "p.db")
    assert fixture_payload.read_text(encoding="utf-8") == before


def test_main_cli_writes_db_and_returns_zero(fixture_payload: Path, tmp_path: Path, capsys) -> None:
    script = _load_script_module()
    db = tmp_path / "p.db"
    code = script.main(["--input", str(fixture_payload), "--market", "NSE", "--db", str(db)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Migrated paper state for market=NSE" in captured.out
    assert SqlitePaperRepo(db, market="NSE").load() is not None


def test_main_cli_errors_on_missing_input(tmp_path: Path, capsys) -> None:
    script = _load_script_module()
    db = tmp_path / "p.db"
    code = script.main(["--input", str(tmp_path / "missing.json"), "--db", str(db)])
    assert code == 2
    err = capsys.readouterr().err
    assert "Input JSON not found" in err
