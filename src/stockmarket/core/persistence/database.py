"""Relational database abstraction: SQLite for local development, PostgreSQL for production."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse


class DatabaseError(Exception):
    pass


class Database(ABC):
    """SQL uses `?` placeholders in every backend; values are always parameterized."""

    dialect: str

    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = (
    )) -> list[dict[str, Any]]: ...

    @abstractmethod
    def transaction(self) -> Iterator[None]:
        """Context manager; re-entrant, the outermost block commits or rolls back."""

    @abstractmethod
    def close(self) -> None: ...

    def healthy(self) -> bool:
        try:
            return self.query("SELECT 1 AS ok")[0]["ok"] == 1
        except Exception:
            return False


class SQLiteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = RLock()
        self._depth = 0

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()


class PostgresDatabase(Database):
    """Requires the optional `psycopg` package; the DSN comes from the environment, never from source."""

    dialect = "postgresql"

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise DatabaseError(
                "PostgreSQL support requires the 'psycopg' package") from exc
        self._conn = psycopg.connect(
            dsn, autocommit=True, row_factory=dict_row)
        self._lock = RLock()
        self._depth = 0

    def __repr__(self) -> str:
        return "PostgresDatabase(<dsn redacted>)"

    @staticmethod
    def _convert(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(self._convert(sql), tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._conn.execute(self._convert(sql), tuple(params)).fetchall())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()


def open_database(url: str) -> Database:
    """`sqlite:///relative.db`, `sqlite:////abs/path.db`, `sqlite://:memory:` or `postgresql://...`."""
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        path = url[len("sqlite:///")
                       :] if url.startswith("sqlite:///") else ":memory:"
        return SQLiteDatabase(path or ":memory:")
    if parsed.scheme in ("postgresql", "postgres"):
        return PostgresDatabase(url)
    raise DatabaseError(f"unsupported database URL scheme: {parsed.scheme!r}")
