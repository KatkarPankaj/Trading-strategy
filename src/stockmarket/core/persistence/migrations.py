"""Versioned, portable schema migrations (SQL valid on both SQLite and PostgreSQL)."""

from __future__ import annotations

from datetime import datetime, timezone

from .database import Database

_V1 = [
    """CREATE TABLE instruments (
        instrument_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
        market TEXT NOT NULL, asset_class TEXT NOT NULL, currency TEXT NOT NULL,
        timezone TEXT NOT NULL, tick_size DOUBLE PRECISION NOT NULL, lot_size INTEGER NOT NULL,
        trading_status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE market_data_metadata (
        id TEXT PRIMARY KEY, provider TEXT NOT NULL, instrument_id TEXT NOT NULL,
        timeframe TEXT NOT NULL, first_timestamp TEXT, last_timestamp TEXT,
        row_count INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE signals (
        signal_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, symbol TEXT NOT NULL,
        timestamp TEXT NOT NULL, strategy TEXT NOT NULL, side TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE risk_decisions (
        decision_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT,
        timestamp TEXT NOT NULL, signal_id TEXT, order_id TEXT, payload TEXT NOT NULL)""",
    """CREATE TABLE strategy_decisions (
        decision_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, instrument_id TEXT NOT NULL,
        symbol TEXT NOT NULL, strategy TEXT NOT NULL, action TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL, timestamp TEXT NOT NULL, explanation TEXT NOT NULL)""",
    """CREATE TABLE orders (
        client_order_id TEXT PRIMARY KEY, broker_order_id TEXT, instrument_id TEXT NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
        order_type TEXT NOT NULL, limit_price DOUBLE PRECISION, stop_price DOUBLE PRECISION,
        timestamp TEXT NOT NULL, strategy TEXT NOT NULL, signal_id TEXT, risk_decision_id TEXT,
        status TEXT NOT NULL, filled_quantity INTEGER NOT NULL, average_fill_price DOUBLE PRECISION,
        error TEXT, updated_at TEXT NOT NULL)""",
    """CREATE TABLE order_events (
        event_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
        timestamp TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL)""",
    """CREATE TABLE fills (
        fill_id TEXT PRIMARY KEY, client_order_id TEXT, instrument_id TEXT NOT NULL,
        side TEXT NOT NULL, quantity INTEGER NOT NULL, price DOUBLE PRECISION NOT NULL,
        fee DOUBLE PRECISION NOT NULL, slippage DOUBLE PRECISION NOT NULL, currency TEXT NOT NULL,
        realized_pnl DOUBLE PRECISION NOT NULL, timestamp TEXT NOT NULL)""",
    """CREATE TABLE positions (
        instrument_id TEXT PRIMARY KEY, side TEXT NOT NULL, quantity INTEGER NOT NULL,
        average_entry_price DOUBLE PRECISION NOT NULL, last_price DOUBLE PRECISION NOT NULL,
        currency TEXT NOT NULL, sector TEXT, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE trades (
        trade_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy TEXT NOT NULL,
        instrument_id TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
        entry_time TEXT NOT NULL, exit_time TEXT NOT NULL, entry_price DOUBLE PRECISION NOT NULL,
        exit_price DOUBLE PRECISION NOT NULL, gross_pnl DOUBLE PRECISION NOT NULL,
        fees DOUBLE PRECISION NOT NULL, net_pnl DOUBLE PRECISION NOT NULL, exit_reason TEXT NOT NULL)""",
    """CREATE TABLE pnl_snapshots (
        snapshot_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, base_currency TEXT NOT NULL,
        equity DOUBLE PRECISION NOT NULL, realized_pnl DOUBLE PRECISION NOT NULL,
        unrealized_pnl DOUBLE PRECISION NOT NULL, fees DOUBLE PRECISION NOT NULL,
        drawdown DOUBLE PRECISION NOT NULL, daily_pnl DOUBLE PRECISION NOT NULL,
        monthly_pnl DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE news_events (
        event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT, source TEXT NOT NULL,
        headline TEXT NOT NULL, event_type TEXT NOT NULL, sentiment TEXT NOT NULL,
        payload TEXT NOT NULL)""",
    """CREATE TABLE ai_analyses (
        analysis_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, analyzed_at TEXT NOT NULL,
        analyzer TEXT, payload TEXT NOT NULL)""",
    """CREATE TABLE system_events (
        event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, component TEXT NOT NULL,
        severity TEXT NOT NULL, message TEXT NOT NULL, correlation_id TEXT, payload TEXT NOT NULL)""",
    """CREATE TABLE audit_log (
        seq BIGINT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
        payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)""",
    "CREATE INDEX idx_signals_instrument_time ON signals (instrument_id, timestamp)",
    "CREATE INDEX idx_orders_status ON orders (status)",
    "CREATE INDEX idx_orders_signal ON orders (signal_id)",
    "CREATE INDEX idx_fills_order ON fills (client_order_id)",
    "CREATE INDEX idx_trades_run ON trades (run_id)",
    "CREATE INDEX idx_system_events_time ON system_events (timestamp)",
]

_V2 = [
    """CREATE TABLE strategy_configs (
        strategy_name TEXT NOT NULL, parameter_version INTEGER NOT NULL, strategy_version TEXT NOT NULL,
        parameters_hash TEXT NOT NULL, parameters TEXT NOT NULL, validation_start TEXT NOT NULL,
        validation_end TEXT NOT NULL, validation_summary TEXT NOT NULL, proposed_by TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL, approved_at TEXT, approved_by TEXT,
        PRIMARY KEY (strategy_name, parameter_version))""",
    "CREATE INDEX idx_strategy_configs_status ON strategy_configs (strategy_name, status)",
]

_V3 = [
    """CREATE TABLE execution_records (
        client_order_id TEXT PRIMARY KEY, mode TEXT NOT NULL, strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL, parameter_version INTEGER, parameters_hash TEXT,
        signal_id TEXT, market_regime TEXT, data_timestamp TEXT, decision_timestamp TEXT NOT NULL,
        versioned INTEGER NOT NULL)""",
    "CREATE INDEX idx_execution_records_strategy ON execution_records (strategy_name, strategy_version)",
]

_V4 = [
    "ALTER TABLE order_events ADD COLUMN fill_quantity INTEGER",
    "ALTER TABLE order_events ADD COLUMN fill_price DOUBLE PRECISION",
    """CREATE TABLE order_audit (
        client_order_id TEXT PRIMARY KEY, market_data TEXT, technical_signals TEXT, news_signals TEXT,
        ai_analysis_ids TEXT, strategy_decision_id TEXT, sizing TEXT, risk_inputs TEXT,
        broker_response TEXT, exit_reason TEXT, data_reference TEXT, updated_at TEXT NOT NULL)""",
]

_V5 = [
    """CREATE TABLE proposal_submissions (
        proposal_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL, operator TEXT NOT NULL, sizing_mode TEXT NOT NULL,
        quantity INTEGER, proposal_as_of TEXT NOT NULL, generated_at TEXT NOT NULL,
        proposal_payload TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    "CREATE INDEX idx_proposal_submissions_state ON proposal_submissions (state)",
]

_V6 = [
    """CREATE TABLE scanner_runs (
        scan_id TEXT PRIMARY KEY, universe_id TEXT NOT NULL, markets TEXT NOT NULL, mode TEXT NOT NULL,
        started_at TEXT NOT NULL, completed_at TEXT NOT NULL, status TEXT NOT NULL,
        requested_count INTEGER NOT NULL, evaluated_count INTEGER NOT NULL,
        accepted_count INTEGER NOT NULL, rejected_count INTEGER NOT NULL,
        failed_count INTEGER NOT NULL, failure_summary TEXT NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE scanner_candidates (
        scan_id TEXT NOT NULL REFERENCES scanner_runs(scan_id),
        instrument_id TEXT NOT NULL, accepted INTEGER NOT NULL, score DOUBLE PRECISION NOT NULL,
        data_timestamp TEXT, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (scan_id, instrument_id))""",
    "CREATE INDEX idx_scanner_runs_universe_time ON scanner_runs (universe_id, started_at)",
    "CREATE INDEX idx_scanner_candidates_scan_score ON scanner_candidates (scan_id, accepted, score)",
]

_V7 = [
    "ALTER TABLE scanner_runs ADD COLUMN as_of TEXT",
    "ALTER TABLE scanner_candidates ADD COLUMN selected INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE research_runs (
        run_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scanner_runs(scan_id),
        as_of TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
        requested_count INTEGER NOT NULL, completed_count INTEGER NOT NULL,
        failed_count INTEGER NOT NULL, failure_summary TEXT NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE research_snapshots (
        snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
        instrument_id TEXT NOT NULL, scanner_rank INTEGER NOT NULL,
        scanner_score DOUBLE PRECISION NOT NULL,
        as_of TEXT NOT NULL, status TEXT NOT NULL,
        payload TEXT NOT NULL)""",
    """CREATE TABLE research_evidence (
        evidence_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL REFERENCES research_snapshots(snapshot_id),
        component TEXT NOT NULL, observed_at TEXT, retrieved_at TEXT NOT NULL,
        source TEXT NOT NULL, quality TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX idx_research_runs_scan_time ON research_runs (scan_id, created_at)",
    "CREATE INDEX idx_research_snapshots_run ON research_snapshots (run_id, scanner_rank)",
    "CREATE INDEX idx_research_evidence_snapshot ON research_evidence (snapshot_id, component)",
]

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "initial_schema", _V1), (2, "strategy_configs",
                                 _V2), (3, "execution_records", _V3),
    (4, "order_audit", _V4), (5, "proposal_submissions", _V5),
    (6, "market_scanner", _V6), (7, "candidate_research", _V7)]


def applied_versions(db: Database) -> set[int]:
    try:
        return {r["version"] for r in db.query("SELECT version FROM schema_migrations")}
    except Exception:  # no migrations table yet means nothing has been applied
        return set()


def pending_migrations(db: Database) -> list[int]:
    done = applied_versions(db)
    return [v for v, _, _ in MIGRATIONS if v not in done]


def migrate(db: Database) -> list[int]:
    """Apply pending migrations in order, each atomically; returns the versions applied."""
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
    done = {r["version"]
            for r in db.query("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, statements in MIGRATIONS:
        if version in done:
            continue
        with db.transaction():
            for sql in statements:
                db.execute(sql)
            db.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                       (version, name, datetime.now(timezone.utc).isoformat()))
        applied.append(version)
    return applied
