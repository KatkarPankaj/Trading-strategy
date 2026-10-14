"""Opt-in only: a disposable stockmarket_test_* PostgreSQL database, never operator state."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import urlparse
from unittest import TestCase, skipUnless

from stockmarket.core.persistence.database import DatabaseError, PostgresDatabase

DSN = os.environ.get("STOCKMARKET_TEST_POSTGRES_DSN", "")


@skipUnless(DSN, "No explicitly supplied disposable PostgreSQL test DSN")
class PostgresIsolationIntegrationTests(TestCase):
    def setUp(self):
        if urlparse(DSN).scheme not in {"postgres", "postgresql"} \
                or not urlparse(DSN).path.lstrip("/").startswith("stockmarket_test_"):
            self.fail("PostgreSQL tests require a disposable stockmarket_test_* database URI")
        self.db = PostgresDatabase(DSN)
        self.addCleanup(self.db.close)

    def test_statement_timeout_and_connection_recovery(self):
        self.db.execute("SET statement_timeout = '50ms'")
        with self.assertRaises(Exception) as raised:
            self.db.query("SELECT pg_sleep(1)")
        self.assertEqual(getattr(raised.exception, "sqlstate", None), "57014")
        self.assertEqual(self.db.query("SELECT 1 AS ok"), [{"ok": 1}])

    def test_transaction_contention_uses_explicit_session_lock_timeout(self):
        other = PostgresDatabase(DSN)
        self.addCleanup(other.close)
        other.execute("SET lock_timeout = '50ms'")
        with self.db.transaction():
            self.db.query("SELECT pg_advisory_xact_lock(878258837)")
            with self.assertRaises(Exception) as raised:
                with other.transaction():
                    other.query("SELECT pg_advisory_xact_lock(878258837)")
            self.assertEqual(getattr(raised.exception, "sqlstate", None), "55P03")
        self.assertEqual(other.query("SELECT 1 AS ok"), [{"ok": 1}])

    def test_mid_transaction_write_rolls_back(self):
        self.db.execute("CREATE TEMP TABLE isolation_fixture (id INTEGER PRIMARY KEY)")
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            with self.db.transaction():
                self.db.execute("INSERT INTO isolation_fixture VALUES (1)")
                raise RuntimeError("controlled interruption")
        self.assertEqual(self.db.query("SELECT * FROM isolation_fixture"), [])

    def test_single_connection_saturation_is_not_pool_isolation(self):
        entered, release = Event(), Event()

        def held():
            with self.db.transaction():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("transaction not released")

        with ThreadPoolExecutor(max_workers=2) as workers:
            owner = workers.submit(held)
            try:
                self.assertTrue(entered.wait(5))
                reader = workers.submit(self.db.query, "SELECT 1 AS ok")
                self.assertFalse(reader.done())
            finally:
                release.set()
            owner.result(timeout=5)
            self.assertEqual(reader.result(timeout=5), [{"ok": 1}])

    def test_validated_replay_is_explicitly_unsupported_without_snapshot_revision(self):
        with self.assertRaisesRegex(DatabaseError, "supported snapshot revision"):
            self.db.history_revision()
