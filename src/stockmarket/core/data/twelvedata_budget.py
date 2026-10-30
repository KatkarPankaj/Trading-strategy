"""Durable, atomic credit reservations for the US Twelve Data allocation."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Generator

from .provider import DataUnavailable, RateLimited


class TwelveDataBudget:
    """A private sidecar, not the trading database or an account-wide usage oracle."""

    def __init__(self, path: str, *, clock: Callable[[], datetime]) -> None:
        if not path.strip() or path == ":memory:":
            raise ValueError("Twelve Data credit ledger requires a persistent file")
        self.path = Path(path)
        self.clock = clock
        try:
            exists = self.path.exists()
            if exists and (not self.path.is_file() or self.path.stat().st_size == 0):
                raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_INVALID")
        except OSError:
            raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_UNAVAILABLE") from None
        with self._transaction(initialize=not exists) as db:
            db.execute("SELECT credits FROM credit_days LIMIT 0")
            db.execute("SELECT at, credits FROM credit_attempts LIMIT 0")

    @contextmanager
    def _transaction(self, *, initialize: bool = False) -> Generator[sqlite3.Connection, None, None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(self.path), timeout=0.25)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if initialize:
                    db.execute("CREATE TABLE IF NOT EXISTS credit_days "
                               "(day TEXT PRIMARY KEY, credits INTEGER NOT NULL CHECK(credits >= 0))")
                    db.execute("CREATE TABLE IF NOT EXISTS credit_attempts "
                               "(at REAL NOT NULL, credits INTEGER NOT NULL CHECK(credits > 0))")
                yield db
        except (sqlite3.Error, OSError):
            raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_UNAVAILABLE") from None

    def _usage(self, db: sqlite3.Connection) -> tuple[int, int, datetime]:
        future = db.execute("SELECT MAX(at) FROM credit_attempts").fetchone()[0]
        # Capture time after establishing the transaction's read snapshot, not before waiting for it.
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DataUnavailable("TWELVEDATA_CREDIT_CLOCK_NOT_AWARE")
        stamp = now.timestamp()
        if future is not None and not isinstance(future, (int, float)):
            raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_INVALID")
        if future is not None and future > stamp:
            raise DataUnavailable("TWELVEDATA_CREDIT_CLOCK_MOVED_BACKWARDS")
        minute = db.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM credit_attempts WHERE at > ?",
            (stamp - 60,)).fetchone()[0]
        row = db.execute("SELECT credits FROM credit_days WHERE day = ?",
                         (now.astimezone(timezone.utc).date().isoformat(),)).fetchone()
        day = row[0] if row else 0
        if not isinstance(minute, int) or minute < 0 or not isinstance(day, int) or day < 0:
            raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_INVALID")
        return minute, day, now

    def usage(self) -> tuple[int, int]:
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=0.25)) as db, db:
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                minute, day, _ = self._usage(db)
                return minute, day
        except sqlite3.Error:
            raise DataUnavailable("TWELVEDATA_CREDIT_LEDGER_UNAVAILABLE") from None

    def reserve(
        self, *, credits: int, minute_limit: int, daily_limit: int,
        daily_baseline: int, reserved_minute: int, reserved_day: int,
    ) -> None:
        with self._transaction() as db:
            minute, day, now = self._usage(db)
            if minute + credits + reserved_minute > minute_limit:
                raise RateLimited(60)
            if day + daily_baseline + credits + reserved_day > daily_limit:
                raise DataUnavailable("TWELVEDATA_DAILY_BUDGET_RESERVED_OR_EXHAUSTED")
            # Charge before transport, including failures/timeouts: never refund uncertain usage.
            stamp = now.timestamp()
            db.execute("DELETE FROM credit_attempts WHERE at <= ?", (stamp - 60,))
            db.execute("INSERT INTO credit_attempts(at, credits) VALUES (?, ?)", (stamp, credits))
            db.execute(
                "INSERT INTO credit_days(day, credits) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET credits = credits + excluded.credits",
                (now.astimezone(timezone.utc).date().isoformat(), credits))
