"""Relational database abstraction: SQLite for local development, PostgreSQL for production."""

from __future__ import annotations

import sqlite3
import math
import time
from collections import deque
from abc import ABC, abstractmethod
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse
from ..paper_worker import PaperWorkerLease


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

    def history_revision(self) -> tuple[int, int]:
        raise DatabaseError("validated history replay requires a supported snapshot revision")


class SQLiteDatabase(Database):
    dialect = "sqlite"

    @property
    def path(self) -> str:
        return self._path

    def claim_paper_worker(self) -> PaperWorkerLease:
        with self._access("paper worker ownership"):
            lease = PaperWorkerLease(self._path)
            self._paper_worker_lease = lease
            return lease

    def __init__(self, path: str = ":memory:", *, lock_timeout: float = 1.0,
                 busy_timeout: float = 1.0, statement_timeout: float = 1.0) -> None:
        for value in (lock_timeout, busy_timeout, statement_timeout):
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("database timeouts must be finite and in (0, 300]")
        self._path = path
        self._paper_worker_lease: PaperWorkerLease | None = None
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False, timeout=busy_timeout)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = RLock()
        self._depth = 0
        self.lock_timeout = lock_timeout
        self.statement_timeout = statement_timeout
        self.timings: deque[dict[str, Any]] = deque(maxlen=100)

    @contextmanager
    def _access(self, operation: str) -> Iterator[None]:
        started = time.perf_counter()
        acquired = self._lock.acquire(timeout=self.lock_timeout)
        entered = time.perf_counter()
        try:
            if not acquired:
                raise DatabaseError("DATABASE_LOCK_TIMEOUT")
            yield
        finally:
            self.timings.append({"operation": operation, "acquired": acquired,
                                 "wait_seconds": entered - started,
                                 "duration_seconds": time.perf_counter() - entered})
            if acquired:
                self._lock.release()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._access("execute"):
            with self._statement():
                self._conn.execute(sql, tuple(params))

    def history_revision(self) -> tuple[int, int]:
        with self._access("history_revision"):
            return (self._conn.execute("PRAGMA data_version").fetchone()[0],
                    self._conn.total_changes)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._access("query"):
            with self._statement():
                return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    @contextmanager
    def _statement(self) -> Iterator[None]:
        deadline = time.perf_counter() + self.statement_timeout
        self._conn.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
        try:
            yield
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted":
                raise DatabaseError("DATABASE_STATEMENT_TIMEOUT") from exc
            raise
        finally:
            self._conn.set_progress_handler(None, 0)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._access("transaction"):
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer and self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    try:
                        self._conn.execute("COMMIT")
                    except BaseException:
                        if self._conn.in_transaction:
                            self._conn.execute("ROLLBACK")
                        raise

    def close(self) -> None:
        with self._access("close"):
            self._conn.close()
            if self._paper_worker_lease is not None:
                self._paper_worker_lease.close()


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
