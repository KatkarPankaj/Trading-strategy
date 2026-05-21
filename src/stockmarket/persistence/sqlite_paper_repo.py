"""SQLite-backed implementation of :class:`PaperRepo`.

Stores the per-market paper-trading state (cash, positions, prices, trade log,
agent memory, daily counters) in a single SQLite database. Multiple markets
coexist in the same file keyed by ``market``; rewrites are wrapped in a single
transaction so a reader never sees a half-written state.

``ui_config`` and ``agent_memory`` are serialised as JSON ``TEXT`` (per the
Phase 9 spec / Handover risk #8) rather than BLOB so the values stay
inspectable with the ``sqlite3`` CLI.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from stockmarket.domain import DailyCounters, PaperState, Position, Side, TradeLogEntry
from stockmarket.persistence.db_storage import open_sqlite_connection

DEFAULT_SQLITE_DB_PATH = Path(".database") / "paper_state.db"

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS paper_state (
        market TEXT PRIMARY KEY,
        cash REAL NOT NULL,
        start_capital REAL NOT NULL,
        realized REAL NOT NULL,
        charges REAL NOT NULL,
        ui_config TEXT NOT NULL,
        agent_memory TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        market TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
        qty INTEGER NOT NULL,
        avg REAL NOT NULL,
        stop REAL NOT NULL,
        target REAL NOT NULL,
        PRIMARY KEY (market, symbol, side)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
        market TEXT NOT NULL,
        symbol TEXT NOT NULL,
        ltp REAL NOT NULL,
        PRIMARY KEY (market, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        charges REAL NOT NULL,
        realized_delta REAL NOT NULL,
        reason TEXT NOT NULL,
        cash_after REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_counters (
        market TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        peak_open_pnl REAL NOT NULL,
        peak_open_pnl_day TEXT NOT NULL,
        profit_guard_triggered_day TEXT NOT NULL,
        profit_ladder_day TEXT NOT NULL,
        profit_ladder_armed INTEGER NOT NULL,
        profit_ladder_pullback_started INTEGER NOT NULL,
        profit_ladder_exited_day TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trade_log_market_id ON trade_log(market, id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market)",
    "CREATE INDEX IF NOT EXISTS idx_prices_market ON prices(market)",
]


class SqlitePaperRepo:
    """Persists one market's paper state in a shared SQLite database."""

    def __init__(self, db_path: Path | str | None = None, market: str = "NSE") -> None:
        self._db_path = Path(db_path) if db_path is not None else DEFAULT_SQLITE_DB_PATH
        self._market = (market or "NSE").upper()
        self._ensure_schema()

    # PaperRepo protocol -----------------------------------------------------

    def path(self) -> Path:
        return self._db_path

    def load(self) -> tuple[PaperState, DailyCounters] | None:
        with self._connect() as conn:
            head = conn.execute(
                "SELECT cash, start_capital, realized, charges, ui_config, agent_memory"
                " FROM paper_state WHERE market = ?",
                (self._market,),
            ).fetchone()
            if head is None:
                return None

            holdings = self._load_positions(conn, "LONG")
            shorts = self._load_positions(conn, "SHORT")
            prices = {
                row["symbol"]: float(row["ltp"])
                for row in conn.execute(
                    "SELECT symbol, ltp FROM prices WHERE market = ?",
                    (self._market,),
                )
            }
            log = [
                _row_to_trade_log_entry(row)
                for row in conn.execute(
                    "SELECT ts, symbol, side, qty, price, charges, realized_delta,"
                    " reason, cash_after FROM trade_log WHERE market = ? ORDER BY id ASC",
                    (self._market,),
                )
            ]
            counters = self._load_counters(conn)

        state = PaperState(
            start_capital=float(head["start_capital"]),
            cash=float(head["cash"]),
            realized=float(head["realized"]),
            charges=float(head["charges"]),
            holdings=holdings,
            shorts=shorts,
            prices=prices,
            log=log,
            ui_config=_loads_json_obj(head["ui_config"]),
            agent_memory=_loads_json_obj(head["agent_memory"]),
            market=self._market,
        )
        return state, counters

    def save(self, state: PaperState, counters: DailyCounters) -> None:
        market = (state.market or self._market).upper()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            with conn:  # transaction
                conn.execute(
                    """
                    INSERT INTO paper_state
                        (market, cash, start_capital, realized, charges,
                         ui_config, agent_memory, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market) DO UPDATE SET
                        cash = excluded.cash,
                        start_capital = excluded.start_capital,
                        realized = excluded.realized,
                        charges = excluded.charges,
                        ui_config = excluded.ui_config,
                        agent_memory = excluded.agent_memory,
                        updated_at = excluded.updated_at
                    """,
                    (
                        market,
                        float(state.cash),
                        float(state.start_capital),
                        float(state.realized),
                        float(state.charges),
                        _dumps_json(state.ui_config),
                        _dumps_json(state.agent_memory),
                        now,
                    ),
                )

                conn.execute("DELETE FROM positions WHERE market = ?", (market,))
                conn.executemany(
                    "INSERT INTO positions (market, symbol, side, qty, avg, stop, target)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (market, symbol, "LONG", p.qty, p.avg, p.stop, p.target)
                        for symbol, p in state.holdings.items()
                    ]
                    + [
                        (market, symbol, "SHORT", p.qty, p.avg, p.stop, p.target)
                        for symbol, p in state.shorts.items()
                    ],
                )

                conn.execute("DELETE FROM prices WHERE market = ?", (market,))
                conn.executemany(
                    "INSERT INTO prices (market, symbol, ltp) VALUES (?, ?, ?)",
                    [(market, str(symbol), float(ltp)) for symbol, ltp in state.prices.items()],
                )

                conn.execute("DELETE FROM trade_log WHERE market = ?", (market,))
                conn.executemany(
                    "INSERT INTO trade_log (market, ts, symbol, side, qty, price,"
                    " charges, realized_delta, reason, cash_after)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [_trade_log_row(market, row) for row in state.log],
                )

                day = counters.peak_open_pnl_day or counters.day
                conn.execute(
                    """
                    INSERT INTO daily_counters
                        (market, day, peak_open_pnl, peak_open_pnl_day,
                         profit_guard_triggered_day, profit_ladder_day,
                         profit_ladder_armed, profit_ladder_pullback_started,
                         profit_ladder_exited_day)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market) DO UPDATE SET
                        day = excluded.day,
                        peak_open_pnl = excluded.peak_open_pnl,
                        peak_open_pnl_day = excluded.peak_open_pnl_day,
                        profit_guard_triggered_day = excluded.profit_guard_triggered_day,
                        profit_ladder_day = excluded.profit_ladder_day,
                        profit_ladder_armed = excluded.profit_ladder_armed,
                        profit_ladder_pullback_started = excluded.profit_ladder_pullback_started,
                        profit_ladder_exited_day = excluded.profit_ladder_exited_day
                    """,
                    (
                        market,
                        day,
                        float(counters.peak_open_pnl),
                        counters.peak_open_pnl_day or day,
                        counters.profit_guard_triggered_day or "",
                        counters.profit_ladder_day or "",
                        1 if counters.profit_ladder_armed else 0,
                        1 if counters.profit_ladder_pullback_started else 0,
                        counters.profit_ladder_exited_day or "",
                    ),
                )

    # internals --------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite_connection(self._db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn:
                for stmt in _SCHEMA:
                    conn.execute(stmt)

    def _load_positions(self, conn: sqlite3.Connection, side: str) -> dict[str, Position]:
        rows = conn.execute(
            "SELECT symbol, qty, avg, stop, target FROM positions"
            " WHERE market = ? AND side = ?",
            (self._market, side),
        )
        return {
            row["symbol"]: Position(
                qty=int(row["qty"]),
                avg=float(row["avg"]),
                stop=float(row["stop"]),
                target=float(row["target"]),
            )
            for row in rows
        }

    def _load_counters(self, conn: sqlite3.Connection) -> DailyCounters:
        row = conn.execute(
            "SELECT day, peak_open_pnl, peak_open_pnl_day,"
            " profit_guard_triggered_day, profit_ladder_day,"
            " profit_ladder_armed, profit_ladder_pullback_started,"
            " profit_ladder_exited_day FROM daily_counters WHERE market = ?",
            (self._market,),
        ).fetchone()
        if row is None:
            return DailyCounters(day="")
        return DailyCounters(
            day=str(row["day"] or ""),
            peak_open_pnl=float(row["peak_open_pnl"]),
            peak_open_pnl_day=str(row["peak_open_pnl_day"] or ""),
            profit_guard_triggered_day=str(row["profit_guard_triggered_day"] or ""),
            profit_ladder_day=str(row["profit_ladder_day"] or ""),
            profit_ladder_armed=bool(row["profit_ladder_armed"]),
            profit_ladder_pullback_started=bool(row["profit_ladder_pullback_started"]),
            profit_ladder_exited_day=str(row["profit_ladder_exited_day"] or ""),
        )


def _trade_log_row(market: str, entry: TradeLogEntry | Mapping[str, Any]) -> tuple:
    data = asdict(entry) if isinstance(entry, TradeLogEntry) else dict(entry)
    return (
        market,
        str(data.get("ts", "")),
        str(data.get("symbol", "")),
        str(data.get("side", "")),
        int(data.get("qty", 0) or 0),
        float(data.get("price", 0.0) or 0.0),
        float(data.get("charges", 0.0) or 0.0),
        float(data.get("realized_delta", 0.0) or 0.0),
        str(data.get("reason", "")),
        float(data.get("cash_after", 0.0) or 0.0),
    )


def _row_to_trade_log_entry(row: sqlite3.Row) -> TradeLogEntry:
    side: Side = str(row["side"]).upper()  # type: ignore[assignment]
    return TradeLogEntry(
        ts=str(row["ts"]),
        symbol=str(row["symbol"]),
        side=side,
        qty=int(row["qty"]),
        price=float(row["price"]),
        charges=float(row["charges"]),
        realized_delta=float(row["realized_delta"]),
        reason=str(row["reason"]),
        cash_after=float(row["cash_after"]),
    )


def _dumps_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _loads_json_obj(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    decoded = json.loads(raw)
    return dict(decoded) if isinstance(decoded, dict) else {}
