"""Persistence layer; import from here, not from core/__init__, to keep core light."""

from .database import Database, DatabaseError, PostgresDatabase, SQLiteDatabase, open_database
from .migrations import MIGRATIONS, migrate, pending_migrations
from .repositories import SecretInPayloadError, Store, to_json


class SchemaOutOfDate(DatabaseError):
    pass


def open_store(url: str, *, migrate_schema: bool = True) -> Store:
    """Open a database from a URL. With migrate_schema=False the schema must already be current."""
    db = open_database(url)
    if migrate_schema:
        migrate(db)
    else:
        pending = pending_migrations(db)
        if pending:
            db.close()
            raise SchemaOutOfDate(
                f"database schema is behind (pending migrations {pending}); run the migrate command")
    return Store(db)


__all__ = [
    "Database",
    "DatabaseError",
    "MIGRATIONS",
    "SchemaOutOfDate",
    "pending_migrations",
    "PostgresDatabase",
    "SQLiteDatabase",
    "SecretInPayloadError",
    "Store",
    "migrate",
    "open_database",
    "open_store",
    "to_json",
]
