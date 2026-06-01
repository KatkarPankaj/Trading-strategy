"""Paper-trading persistence port and SQLite factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from stockmarket.domain.types import DailyCounters, PaperState

_DATABASE_CONFIG_PATH = Path("config") / "database_config.json"


class PaperRepo(Protocol):
    def load(self) -> tuple[PaperState, DailyCounters] | None: ...

    def save(self, state: PaperState, counters: DailyCounters) -> None: ...

    def path(self) -> Path: ...


def get_paper_repo(path: Path | None = None, market: str = "NSE") -> PaperRepo:
    """Construct the SQLite :class:`PaperRepo` for ``market``.

    ``path`` is accepted for compatibility with older callers but ignored; the
    database path comes from ``storage.paper_state_database_path`` or the
    SQLite default.
    """
    normalized_market = (market or "NSE").upper()
    return _build_sqlite_repo(normalized_market)


def _build_sqlite_repo(market: str) -> PaperRepo:
    from stockmarket.persistence.sqlite_paper_repo import SqlitePaperRepo

    return SqlitePaperRepo(_resolve_sqlite_db_path(), market=market)


def _resolve_sqlite_db_path() -> Path:
    from stockmarket.persistence.sqlite_paper_repo import DEFAULT_SQLITE_DB_PATH

    cfg = _read_database_config()
    storage = cfg.get("storage") if isinstance(cfg.get("storage"), dict) else {}
    configured = storage.get("paper_state_database_path") if storage else None
    return Path(configured) if configured else DEFAULT_SQLITE_DB_PATH


def _read_database_config() -> dict:
    if not _DATABASE_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_DATABASE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
