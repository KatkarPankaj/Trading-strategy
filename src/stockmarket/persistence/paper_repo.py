"""Paper-trading persistence port and backend selection factory."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from stockmarket.domain.types import DailyCounters, PaperState

_DEFAULT_STATE_FILES = {
    "NSE": Path("outputs") / "simple_paper_state.json",
    "US": Path("outputs") / "simple_paper_state_us.json",
}
_DATABASE_CONFIG_PATH = Path("config") / "database_config.json"
_VALID_BACKENDS = ("json", "sqlite")


class PaperRepo(Protocol):
    def load(self) -> tuple[PaperState, DailyCounters] | None: ...

    def save(self, state: PaperState, counters: DailyCounters) -> None: ...

    def path(self) -> Path: ...


def get_paper_repo(path: Path | None = None, market: str = "NSE") -> PaperRepo:
    """Construct the active :class:`PaperRepo` for ``market``.

    Backend selection is controlled by the ``PAPER_REPO_BACKEND`` environment
    variable (``json`` or ``sqlite``); when unset, the ``paper_repo_backend``
    field in ``config/database_config.json`` is consulted and finally falls
    back to ``json`` to preserve phase-2 behavior.

    During the migration period (Handover risk #7) two transitional flags wrap
    the SQLite backend:

    * ``PAPER_REPO_FALLBACK_JSON=1`` — on a SQLite miss, read from the JSON
      file at ``path`` (or the per-market default) before returning ``None``.
    * ``PAPER_REPO_DUAL_WRITE=1`` — every save also writes the JSON file so
      the legacy on-disk format stays current for one release.
    """

    normalized_market = (market or "NSE").upper()
    json_path = Path(path) if path is not None else _default_json_path(normalized_market)
    json_repo = _build_json_repo(json_path, normalized_market)

    backend = _resolve_backend()
    if backend == "json":
        return json_repo

    sqlite_repo = _build_sqlite_repo(normalized_market)
    if not _flag_enabled("PAPER_REPO_FALLBACK_JSON") and not _flag_enabled("PAPER_REPO_DUAL_WRITE"):
        return sqlite_repo

    return _DualWriteSqlitePaperRepo(
        primary=sqlite_repo,
        json_repo=json_repo,
        fallback_on_miss=_flag_enabled("PAPER_REPO_FALLBACK_JSON"),
        dual_write=_flag_enabled("PAPER_REPO_DUAL_WRITE"),
    )


def _default_json_path(market: str) -> Path:
    return _DEFAULT_STATE_FILES.get(market, _DEFAULT_STATE_FILES["NSE"])


def _build_json_repo(json_path: Path, market: str) -> PaperRepo:
    from stockmarket.persistence.json_paper_repo import JsonPaperRepo

    return JsonPaperRepo(json_path, market=market)


def _build_sqlite_repo(market: str) -> PaperRepo:
    from stockmarket.persistence.sqlite_paper_repo import SqlitePaperRepo

    return SqlitePaperRepo(_resolve_sqlite_db_path(), market=market)


def _resolve_backend() -> str:
    env_val = os.environ.get("PAPER_REPO_BACKEND")
    if env_val is not None:
        normalized = env_val.strip().lower()
        if normalized in _VALID_BACKENDS:
            return normalized
        raise ValueError(
            f"PAPER_REPO_BACKEND={env_val!r} is invalid; expected one of {_VALID_BACKENDS}"
        )

    cfg = _read_database_config()
    cfg_val = str(cfg.get("paper_repo_backend") or "").strip().lower()
    if cfg_val in _VALID_BACKENDS:
        return cfg_val
    if cfg_val:
        raise ValueError(
            f"paper_repo_backend={cfg_val!r} in {_DATABASE_CONFIG_PATH} is invalid;"
            f" expected one of {_VALID_BACKENDS}"
        )
    return "json"


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


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


class _DualWriteSqlitePaperRepo:
    """SQLite primary repo with optional JSON read fallback and dual write."""

    def __init__(
        self,
        *,
        primary: PaperRepo,
        json_repo: PaperRepo,
        fallback_on_miss: bool,
        dual_write: bool,
    ) -> None:
        self._primary = primary
        self._json = json_repo
        self._fallback_on_miss = fallback_on_miss
        self._dual_write = dual_write

    def load(self) -> tuple[PaperState, DailyCounters] | None:
        result = self._primary.load()
        if result is not None:
            return result
        if self._fallback_on_miss:
            return self._json.load()
        return None

    def save(self, state: PaperState, counters: DailyCounters) -> None:
        self._primary.save(state, counters)
        if self._dual_write:
            self._json.save(state, counters)

    def path(self) -> Path:
        return self._primary.path()
