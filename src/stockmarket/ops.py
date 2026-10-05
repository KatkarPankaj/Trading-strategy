"""Operations commands: python -m stockmarket.ops {migrate,status,backup,verify-audit,check-config}."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess  # noqa: S404 - fixed argument list, never a shell
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import unquote, urlparse

from .core.persistence import open_database
from .core.persistence.migrations import MIGRATIONS, applied_versions, migrate, pending_migrations
from .core.persistence.repositories import AuditRepository
from .core.security import SecurityError, get_secret, redact_dsn

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def database_url(env: Mapping[str, str] | None = None) -> str:
    """DATABASE_URL, or the contents of the file named by DATABASE_URL_FILE (container secret)."""
    secret = get_secret("DATABASE_URL", os.environ if env is None else env)
    assert secret is not None
    return secret.reveal()


def _prune(directory: Path, keep: int, pattern: str) -> list[Path]:
    files = sorted(directory.glob(pattern),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = files[keep:]
    for old in removed:
        old.unlink()
    return removed


def backup(url: str, directory: Path, keep: int) -> Path:
    """SQLite: online backup API. PostgreSQL: pg_dump custom format; the password travels in the environment only."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else ""
        if not path:
            raise ValueError(
                "only file-backed SQLite databases can be backed up")
        target = directory / f"trading-{stamp}.db"
        source, dest = sqlite3.connect(path), sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        _prune(directory, keep, "trading-*.db")
        return target
    if parsed.scheme in ("postgresql", "postgres"):
        user, name = unquote(parsed.username or ""), parsed.path.lstrip("/")
        host = parsed.hostname or ""
        for label, value in (("user", user), ("database", name), ("host", host)):
            if not _SAFE_NAME.match(value):
                raise ValueError(f"unsafe {label} in database URL")
        pg_dump = shutil.which("pg_dump")
        if pg_dump is None:
            raise RuntimeError("pg_dump is not installed on this host")
        target = directory / f"trading-{stamp}.dump"
        env = {**os.environ, "PGPASSWORD": unquote(parsed.password or "")}
        subprocess.run(  # noqa: S603
            [pg_dump, "--format=custom", "--no-owner", "-h", host, "-p", str(parsed.port or 5432),
             "-U", user, "-f", str(target), name],
            env=env, check=True, shell=False, timeout=3600)
        _prune(directory, keep, "trading-*.dump")
        return target
    raise ValueError(f"unsupported database scheme {parsed.scheme!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockmarket.ops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending schema migrations")
    sub.add_parser("status", help="show applied and pending migrations")
    b = sub.add_parser(
        "backup", help="write a database backup and prune old ones")
    b.add_argument("--dir", default="backups", type=Path)
    b.add_argument("--keep", default=14, type=int)
    sub.add_parser("verify-audit", help="verify the audit log hash chain")
    sub.add_parser(
        "check-config", help="validate environment configuration and print it with secrets masked")
    args = parser.parse_args(argv)

    try:
        if args.command == "check-config":
            from .core.settings import ConfigurationError, load_settings

            try:
                print(json.dumps(load_settings().to_public_dict(),
                      indent=2, sort_keys=True))
            except ConfigurationError as exc:
                print(exc, file=sys.stderr)
                return 1
            return 0

        url = database_url()
        if args.command == "backup":
            print(backup(url, args.dir, args.keep))
            return 0
        db = open_database(url)
        try:
            if args.command == "migrate":
                applied = migrate(db)
                print(
                    f"applied: {applied or 'none'}; current schema version {max(v for v, _, _ in MIGRATIONS)}")
            elif args.command == "status":
                print(json.dumps({"database": redact_dsn(url), "applied": sorted(applied_versions(db)),
                                  "pending": pending_migrations(db)}, indent=2))
                return 1 if pending_migrations(db) else 0
            elif args.command == "verify-audit":
                problems = AuditRepository(db).verify_chain()
                print("audit chain intact" if not problems else "\n".join(problems))
                return 1 if problems else 0
        finally:
            db.close()
    except (SecurityError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
