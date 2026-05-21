"""One-shot migration: JSON paper-state file -> SQLite paper_state.db.

Idempotent and non-destructive. Re-runs upsert the market row in the SQLite
database; the source JSON file is left untouched.

Usage:

    python scripts/migrate_paper_state_json_to_sqlite.py \\
        --input outputs/simple_paper_state.json \\
        --market NSE \\
        --db .database/paper_state.db

The ``--db`` flag defaults to the spec path (``.database/paper_state.db``); it
can also be supplied via ``PAPER_REPO_DB_PATH`` for scripted re-runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from stockmarket.persistence.json_paper_repo import JsonPaperRepo  # noqa: E402
from stockmarket.persistence.sqlite_paper_repo import (  # noqa: E402
    DEFAULT_SQLITE_DB_PATH,
    SqlitePaperRepo,
)


@dataclass(frozen=True)
class MigrationSummary:
    market: str
    input_path: Path
    db_path: Path
    holdings: int
    shorts: int
    prices: int
    log_rows: int
    counters_written: bool

    def format(self) -> str:
        return (
            f"Migrated paper state for market={self.market}\n"
            f"  source : {self.input_path}\n"
            f"  target : {self.db_path}\n"
            f"  positions: {self.holdings} long, {self.shorts} short\n"
            f"  prices   : {self.prices}\n"
            f"  log rows : {self.log_rows}\n"
            f"  counters : {'written' if self.counters_written else 'skipped'}\n"
        )


def migrate(input_path: Path, market: str, db_path: Path) -> MigrationSummary:
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_path}")

    json_repo = JsonPaperRepo(input_path, market=market)
    loaded = json_repo.load()
    if loaded is None:
        raise ValueError(f"Input JSON {input_path} is empty or unreadable")

    state, counters = loaded
    sqlite_repo = SqlitePaperRepo(db_path, market=market)
    sqlite_repo.save(state, counters)

    return MigrationSummary(
        market=market.upper(),
        input_path=input_path,
        db_path=db_path,
        holdings=len(state.holdings),
        shorts=len(state.shorts),
        prices=len(state.prices),
        log_rows=len(state.log),
        counters_written=bool(counters.day or counters.peak_open_pnl_day),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Path to source JSON paper-state file")
    parser.add_argument("--market", default="NSE", help="Market identifier (NSE, US, ...). Default: NSE")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("PAPER_REPO_DB_PATH") or DEFAULT_SQLITE_DB_PATH),
        help=f"Destination SQLite database path (default: {DEFAULT_SQLITE_DB_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        summary = migrate(args.input, args.market, args.db)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(summary.format())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
