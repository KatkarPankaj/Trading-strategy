"""Repositories over the relational schema. Domain objects go in and out; secrets are refused."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from .database import Database

_SECRET_MARKERS = ("api_key", "apikey", "secret", "password", "passwd", "token",
                   "authorization", "credential", "private_key")
GENESIS_HASH = "0" * 64


class SecretInPayloadError(ValueError):
    """A payload key looks like a credential; credentials must never be persisted."""


def _encode(value: Any) -> Any:
    if isinstance(value, (UUID, date, datetime, time)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in fields(value)}
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _check_keys(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if any(m in str(key).lower() for m in _SECRET_MARKERS):
                raise SecretInPayloadError(
                    f"refusing to persist secret-like key '{path}{key}'")
            _check_keys(inner, f"{path}{key}.")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_keys(item, path)


def to_json(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True,
                      separators=(",", ":"), default=_encode)
    _check_keys(json.loads(text))
    return text


def ts(value: datetime | None) -> str | None:
    """Timezone-aware datetimes are stored as UTC ISO-8601 text."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


class _Repository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _upsert(self, table: str, key: str, row: Mapping[str, Any]) -> None:
        cols = list(row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key)
        self._db.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT({key}) DO UPDATE SET {updates}",
            [row[c] for c in cols])


class InstrumentRepository(_Repository):
    def save(self, i: Any) -> None:
        self._upsert("instruments", "instrument_id", {
            "instrument_id": i.instrument_id, "symbol": i.symbol, "exchange": i.exchange,
            "market": i.market, "asset_class": i.asset_class.value, "currency": i.currency,
            "timezone": i.timezone, "tick_size": i.tick_size, "lot_size": i.lot_size,
            "trading_status": i.trading_status.value,
            "payload": to_json({
                "trading_hours": i.trading_hours, "price_precision": i.price_precision,
                "minimum_order_quantity": i.minimum_order_quantity, "shortable": i.shortable,
                "name": i.name, "country": i.country, "sector": i.sector,
                "industry": i.industry, "isin": i.isin, "figi": i.figi,
                "cusip": i.cusip, "mic": i.mic, "exchange_symbol": i.exchange_symbol,
                "provider_symbol": i.provider_symbol, "active": i.active,
                "tradable": i.tradable, "market_cap": i.market_cap}),
            "updated_at": ts(datetime.now(timezone.utc))})

    def get(self, instrument_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM instruments WHERE instrument_id = ?", (instrument_id,))
        return rows[0] if rows else None

    def list(self, market: str | None = None) -> list[dict[str, Any]]:
        if market:
            return self._db.query("SELECT * FROM instruments WHERE market = ? ORDER BY symbol", (market,))
        return self._db.query("SELECT * FROM instruments ORDER BY market, symbol")


class MarketDataMetadataRepository(_Repository):
    def save(self, provider: str, instrument_id: str, timeframe: str, *,
             first_timestamp: datetime | None, last_timestamp: datetime | None, row_count: int) -> None:
        self._upsert("market_data_metadata", "id", {
            "id": f"{provider}|{instrument_id}|{timeframe}", "provider": provider,
            "instrument_id": instrument_id, "timeframe": timeframe,
            "first_timestamp": ts(first_timestamp), "last_timestamp": ts(last_timestamp),
            "row_count": row_count, "updated_at": ts(datetime.now(timezone.utc))})

    def last_timestamp(self, provider: str, instrument_id: str, timeframe: str) -> str | None:
        rows = self._db.query("SELECT last_timestamp FROM market_data_metadata WHERE id = ?",
                              (f"{provider}|{instrument_id}|{timeframe}",))
        return rows[0]["last_timestamp"] if rows else None


class SignalRepository(_Repository):
    def get(self, signal_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        return rows[0] if rows else None

    def save(self, s: Any) -> None:
        self._upsert("signals", "signal_id", {
            "signal_id": str(s.signal_id), "instrument_id": s.instrument_id, "symbol": s.symbol,
            "timestamp": ts(s.timestamp), "strategy": s.strategy, "side": s.side.value,
            "confidence": s.confidence,
            "payload": to_json({
                "entry_price": s.entry_price, "stop_loss": s.stop_loss, "take_profit": s.take_profit,
                "reward_risk": s.reward_risk, "expected_edge": s.expected_edge, "regime": s.regime,
                "reasons": s.reasons, "invalidation_conditions": s.invalidation_conditions})})

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,))


class RiskDecisionRepository(_Repository):
    def save(self, d: Any, inputs: Mapping[str, Any] | None = None) -> None:
        self._upsert("risk_decisions", "decision_id", {
            "decision_id": str(d.decision_id), "status": d.status.value, "reason": d.reason,
            "timestamp": ts(d.timestamp), "signal_id": _opt(d.signal_id), "order_id": _opt(d.order_id),
            "payload": to_json(inputs or {})})

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM risk_decisions ORDER BY timestamp DESC LIMIT ?", (limit,))

    def get(self, decision_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM risk_decisions WHERE decision_id = ?", (decision_id,))
        return rows[0] if rows else None


class StrategyDecisionRepository(_Repository):
    def get(self, decision_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM strategy_decisions WHERE decision_id = ?", (decision_id,))
        return rows[0] if rows else None

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM strategy_decisions ORDER BY timestamp DESC LIMIT ?", (limit,))

    def save(self, d: Any) -> None:
        self._upsert("strategy_decisions", "decision_id", {
            "decision_id": str(d.decision_id), "input_hash": d.input_hash,
            "instrument_id": d.instrument_id, "symbol": d.symbol, "strategy": d.strategy,
            "action": d.action.value, "confidence": d.confidence, "timestamp": ts(d.timestamp),
            "explanation": to_json(d.explanation)})


class OrderRepository(_Repository):
    def save(self, o: Any) -> None:
        self._upsert("orders", "client_order_id", {
            "client_order_id": o.client_order_id, "broker_order_id": o.broker_order_id,
            "instrument_id": o.instrument_id, "symbol": o.symbol, "side": o.side.value,
            "quantity": o.quantity, "order_type": o.order_type.value, "limit_price": o.limit_price,
            "stop_price": o.stop_price, "timestamp": ts(o.timestamp), "strategy": o.strategy,
            "signal_id": _opt(o.signal_id), "risk_decision_id": _opt(o.risk_decision_id),
            "status": o.status.value, "filled_quantity": o.filled_quantity,
            "average_fill_price": o.average_fill_price, "error": o.error,
            "updated_at": ts(datetime.now(timezone.utc))})

    def save_with_events(self, order: Any, events: Iterable[Any]) -> None:
        """Order row plus its full transition history, atomically; replays are idempotent."""
        with self._db.transaction():
            self.save(order)
            for n, e in enumerate(events):
                self._upsert("order_events", "event_id", {
                    "event_id": f"{order.client_order_id}:{n}",
                    "client_order_id": order.client_order_id, "timestamp": ts(e.timestamp),
                    "from_status": e.from_status.value if e.from_status else None,
                    "to_status": e.to_status.value, "detail": e.detail,
                    "fill_quantity": e.fill_quantity, "fill_price": e.fill_price})

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return rows[0] if rows else None

    def all(self) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM orders ORDER BY timestamp")

    def open_orders(self) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM orders WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','FAILED') "
            "ORDER BY timestamp")

    def events(self, client_order_id: str) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM order_events WHERE client_order_id = ? ORDER BY timestamp, event_id",
                              (client_order_id,))


class FillRepository(_Repository):
    def save(self, f: Any, client_order_id: str | None = None) -> str:
        fill_id = str(uuid4())
        self._upsert("fills", "fill_id", {
            "fill_id": fill_id, "client_order_id": client_order_id, "instrument_id": f.instrument_id,
            "side": f.side.value, "quantity": f.quantity, "price": f.price, "fee": f.fee,
            "slippage": f.slippage, "currency": f.currency, "realized_pnl": f.realized_pnl,
            "timestamp": ts(f.timestamp)})
        return fill_id

    def all(self) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM fills ORDER BY timestamp")


class PositionRepository(_Repository):
    def replace_all(self, positions: Iterable[Any], as_of: datetime) -> None:
        """Current-state table: the persisted set always equals the supplied set."""
        with self._db.transaction():
            self._db.execute("DELETE FROM positions")
            for p in positions:
                self._upsert("positions", "instrument_id", {
                    "instrument_id": p.instrument_id, "side": p.side.value, "quantity": p.quantity,
                    "average_entry_price": p.average_entry_price, "last_price": p.last_price,
                    "currency": p.currency, "sector": p.sector, "opened_at": ts(p.opened_at),
                    "updated_at": ts(as_of)})

    def all(self) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM positions ORDER BY instrument_id")


class TradeRepository(_Repository):
    def save(self, t: Any, *, run_id: str, strategy: str) -> str:
        trade_id = str(uuid4())
        self._upsert("trades", "trade_id", {
            "trade_id": trade_id, "run_id": run_id, "strategy": strategy,
            "instrument_id": t.instrument_id, "side": t.side.value, "quantity": t.quantity,
            "entry_time": ts(t.entry_time), "exit_time": ts(t.exit_time),
            "entry_price": t.entry_price, "exit_price": t.exit_price, "gross_pnl": t.gross_pnl,
            "fees": t.fees, "net_pnl": t.net_pnl, "exit_reason": t.exit_reason})
        return trade_id

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM trades WHERE run_id = ? ORDER BY entry_time", (run_id,))

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,))


class PnLRepository(_Repository):
    def snapshot(self, portfolio: Any, timestamp: datetime) -> str:
        snapshot_id = str(uuid4())
        self._upsert("pnl_snapshots", "snapshot_id", {
            "snapshot_id": snapshot_id, "timestamp": ts(timestamp),
            "base_currency": portfolio.base_currency, "equity": portfolio.equity,
            "realized_pnl": portfolio.realized_pnl, "unrealized_pnl": portfolio.unrealized_pnl,
            "fees": portfolio.fees, "drawdown": portfolio.drawdown,
            "daily_pnl": portfolio.daily_pnl, "monthly_pnl": portfolio.monthly_pnl})
        return snapshot_id

    def latest(self) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM pnl_snapshots ORDER BY timestamp DESC LIMIT 1")
        return rows[0] if rows else None

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM pnl_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,))


class NewsRepository(_Repository):
    def analyses_by_id(self, ids: Iterable[str]) -> list[dict[str, Any]]:
        found = []
        for analysis_id in ids:
            found += self._db.query(
                "SELECT * FROM ai_analyses WHERE analysis_id = ?", (analysis_id,))
        return found

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM news_events ORDER BY timestamp DESC LIMIT ?", (limit,))

    def recent_analyses(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM ai_analyses ORDER BY analyzed_at DESC LIMIT ?", (limit,))

    def save_event(self, e: Any) -> None:
        self._upsert("news_events", "event_id", {
            "event_id": str(e.event_id), "timestamp": ts(e.timestamp), "symbol": e.symbol,
            "source": e.source, "headline": e.headline, "event_type": e.event_type.value,
            "sentiment": e.sentiment.value,
            "payload": to_json({
                "content": e.content, "reference": e.reference,
                "sentiment_confidence": e.sentiment_confidence, "market_impact": e.market_impact,
                "relevance": e.relevance, "affected_sector": e.affected_sector,
                "affected_market": e.affected_market, "provider_event_id": e.provider_event_id})})

    def save_analysis(self, a: Any) -> None:
        self._upsert("ai_analyses", "analysis_id", {
            "analysis_id": str(a.analysis_id), "event_id": str(a.event_id),
            "analyzed_at": ts(a.analyzed_at), "analyzer": a.analyzer,
            "payload": to_json({
                "summary": a.summary, "event_type": a.event_type, "sentiment": a.sentiment,
                "sentiment_confidence": a.sentiment_confidence, "market_impact": a.market_impact,
                "relevance": a.relevance, "key_points": a.key_points, "risks": a.risks})})


class SystemEventRepository(_Repository):
    def record(self, component: str, severity: str, message: str, *, timestamp: datetime | None = None,
               correlation_id: str | None = None, payload: Mapping[str, Any] | None = None) -> str:
        event_id = str(uuid4())
        self._upsert("system_events", "event_id", {
            "event_id": event_id, "timestamp": ts(timestamp or datetime.now(timezone.utc)),
            "component": component, "severity": severity, "message": message,
            "correlation_id": correlation_id, "payload": to_json(payload or {})})
        return event_id

    def recent(self, limit: int = 100, severity: str | None = None) -> list[dict[str, Any]]:
        if severity:
            return self._db.query("SELECT * FROM system_events WHERE severity = ? ORDER BY timestamp DESC LIMIT ?",
                                  (severity, limit))
        return self._db.query("SELECT * FROM system_events ORDER BY timestamp DESC LIMIT ?", (limit,))


class AuditRepository(_Repository):
    """Append-only and hash-chained, so edits or deletions of past entries are detectable."""

    def append(self, actor: str, action: str, entity_type: str, entity_id: str,
               payload: Mapping[str, Any] | None = None, *, timestamp: datetime | None = None) -> int:
        body = to_json(payload or {})
        stamp = ts(timestamp or datetime.now(timezone.utc))
        event_id = str(uuid4())
        with self._db.transaction():
            last = self._db.query(
                "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")
            seq = (last[0]["seq"] + 1) if last else 1
            prev = last[0]["hash"] if last else GENESIS_HASH
            digest = self._digest(prev, seq, event_id, stamp,
                                  actor, action, entity_type, entity_id, body)
            self._db.execute(
                "INSERT INTO audit_log (seq, event_id, timestamp, actor, action, entity_type, entity_id, "
                "payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, event_id, stamp, actor, action, entity_type, entity_id, body, prev, digest))
        return seq

    @staticmethod
    def _digest(prev: str, *parts: Any) -> str:
        return hashlib.sha256(json.dumps([prev, *parts]).encode("utf-8")).hexdigest()

    def entries(self, entity_id: str | None = None) -> list[dict[str, Any]]:
        if entity_id:
            return self._db.query("SELECT * FROM audit_log WHERE entity_id = ? ORDER BY seq", (entity_id,))
        return self._db.query("SELECT * FROM audit_log ORDER BY seq")

    def latest(self, actions: Iterable[str]) -> dict[str, Any] | None:
        names = list(actions)
        rows = self._db.query(
            f"SELECT * FROM audit_log WHERE action IN ({', '.join('?' for _ in names)}) ORDER BY seq DESC LIMIT 1",
            names)
        return rows[0] if rows else None

    def verify_chain(self) -> list[str]:
        """Return a description of every inconsistency found (empty means intact)."""
        problems, prev, expected = [], GENESIS_HASH, 1
        for r in self.entries():
            if r["seq"] != expected:
                problems.append(f"sequence gap before seq {r['seq']}")
                expected = r["seq"]
            digest = self._digest(prev, r["seq"], r["event_id"], r["timestamp"], r["actor"], r["action"],
                                  r["entity_type"], r["entity_id"], r["payload"])
            if r["prev_hash"] != prev or r["hash"] != digest:
                problems.append(f"hash mismatch at seq {r['seq']}")
            prev, expected = r["hash"], expected + 1
        return problems


class StrategyConfigRepository(_Repository):
    def save(self, c: Any) -> None:
        with self._db.transaction():
            self._db.execute(
                "DELETE FROM strategy_configs WHERE strategy_name = ? AND parameter_version = ?",
                (c.strategy_name, c.parameter_version))
            self._db.execute(
                "INSERT INTO strategy_configs (strategy_name, parameter_version, strategy_version, parameters_hash, "
                "parameters, validation_start, validation_end, validation_summary, proposed_by, created_at, status, "
                "approved_at, approved_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c.strategy_name, c.parameter_version, c.strategy_version, c.parameters_hash, to_json(c.parameters),
                 c.validation_period[0].isoformat(
                ), c.validation_period[1].isoformat(),
                    to_json(c.validation_summary), c.proposed_by, ts(
                     c.created_at), c.status.value,
                    ts(c.approved_at), c.approved_by))

    def load_all(self) -> list[Any]:
        from ..learning import ConfigStatus, LiveStrategyConfig

        out = []
        for r in self._db.query("SELECT * FROM strategy_configs ORDER BY strategy_name, parameter_version"):
            out.append(LiveStrategyConfig(
                r["strategy_name"], r["strategy_version"], r["parameter_version"], json.loads(
                    r["parameters"]),
                r["parameters_hash"], (date.fromisoformat(
                    r["validation_start"]), date.fromisoformat(r["validation_end"])),
                json.loads(r["validation_summary"]), r["proposed_by"], datetime.fromisoformat(
                    r["created_at"]),
                ConfigStatus(r["status"]),
                datetime.fromisoformat(r["approved_at"]) if r["approved_at"] else None, r["approved_by"]))
        return out


class OrderAuditRepository(_Repository):
    """Decision inputs per order; sections arrive at different times and are merged."""

    _JSON = ("market_data", "technical_signals", "news_signals", "ai_analysis_ids", "sizing",
             "risk_inputs", "broker_response")
    _TEXT = ("strategy_decision_id", "exit_reason", "data_reference")

    def record(self, client_order_id: str, **sections: Any) -> None:
        unknown = set(sections) - set(self._JSON) - set(self._TEXT)
        if unknown:
            raise ValueError(f"unknown audit sections: {sorted(unknown)}")
        with self._db.transaction():
            rows = self._db.query(
                "SELECT * FROM order_audit WHERE client_order_id = ?", (client_order_id,))
            row = dict(rows[0]) if rows else {
                c: None for c in (*self._JSON, *self._TEXT)}
            for name, value in sections.items():
                if value is None:
                    continue
                row[name] = to_json(
                    value) if name in self._JSON else str(value)
            row["client_order_id"] = client_order_id
            row["updated_at"] = ts(datetime.now(timezone.utc))
            self._upsert("order_audit", "client_order_id", row)

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM order_audit WHERE client_order_id = ?", (client_order_id,))
        if not rows:
            return None
        out = dict(rows[0])
        for name in self._JSON:
            out[name] = json.loads(out[name]) if out[name] else None
        return out


class ExecutionRecordRepository(_Repository):
    """Which strategy version, parameters, regime and data produced each order."""

    def save(self, *, client_order_id: str, mode: str, strategy_name: str, strategy_version: str,
             parameter_version: int | None, parameters_hash: str | None, signal_id: Any,
             market_regime: str | None, data_timestamp: datetime | None, decision_timestamp: datetime,
             versioned: bool) -> None:
        self._upsert("execution_records", "client_order_id", {
            "client_order_id": client_order_id, "mode": mode, "strategy_name": strategy_name,
            "strategy_version": strategy_version, "parameter_version": parameter_version,
            "parameters_hash": parameters_hash, "signal_id": _opt(signal_id), "market_regime": market_regime,
            "data_timestamp": ts(data_timestamp), "decision_timestamp": ts(decision_timestamp),
            "versioned": 1 if versioned else 0})

    def paper_test_stats(self) -> dict[str, int]:
        """Evidence that the strategy ran in paper mode: filled paper orders and distinct decision days."""
        rows = self._db.query(
            "SELECT COUNT(*) AS filled, COUNT(DISTINCT SUBSTR(e.decision_timestamp, 1, 10)) AS days "
            "FROM execution_records e JOIN orders o ON o.client_order_id = e.client_order_id "
            "WHERE e.mode = 'PAPER' AND o.status = 'FILLED'")
        return {"filled_orders": int(rows[0]["filled"]), "trading_days": int(rows[0]["days"])}

    def for_order(self, client_order_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM execution_records WHERE client_order_id = ?", (client_order_id,))
        return rows[0] if rows else None

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._db.query("SELECT * FROM execution_records ORDER BY decision_timestamp DESC LIMIT ?", (limit,))


class ScannerRunRepository(_Repository):
    """Durable scanner runs and candidate audit data, isolated from order persistence."""

    def save_scan(self, result: Any, *, candidates: Iterable[Any] | None = None) -> None:
        candidate_rows = tuple(result.candidates if candidates is None else candidates)
        with self._db.transaction():
            self._db.execute(
                """INSERT INTO scanner_runs
                   (scan_id, universe_id, markets, mode, started_at, completed_at, status,
                    requested_count, evaluated_count, accepted_count, rejected_count,
                    failed_count, failure_summary, payload, as_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.scan_id, result.universe_id, to_json(result.markets),
                    result.mode.value,
                    ts(result.started_at), ts(result.completed_at), result.status.value,
                    result.requested_count, result.evaluated_count, result.accepted_count,
                    result.rejected_count, result.failed_count, to_json(result.failure_summary),
                    to_json({"scan_id": result.scan_id, "universe_id": result.universe_id,
                             "mode": result.mode, "status": result.status}),
                    ts(result.as_of or result.started_at),
                ),
            )
            accepted_ids = {
                candidate.instrument_id for candidate in candidate_rows
                if not candidate.rejection_reasons and not candidate.evaluation_failed
            }
            selected_ids = {candidate.instrument_id for candidate in result.candidates}
            for candidate in candidate_rows:
                self._db.execute(
                    """INSERT INTO scanner_candidates
                       (scan_id, instrument_id, accepted, selected, score, data_timestamp,
                        quality_status, payload)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.scan_id, candidate.instrument_id,
                        1 if candidate.instrument_id in accepted_ids else 0,
                        1 if candidate.instrument_id in selected_ids else 0,
                        candidate.preliminary_score, ts(candidate.data_timestamp),
                        candidate.data_quality.value, to_json(candidate),
                    ),
                )

    def get(self, scan_id: str) -> dict[str, Any] | None:
        rows = self._db.query("SELECT * FROM scanner_runs WHERE scan_id = ?", (scan_id,))
        if not rows:
            return None
        row = rows[0]
        row["markets"] = json.loads(row["markets"])
        row["failure_summary"] = json.loads(row["failure_summary"])
        row["payload"] = json.loads(row["payload"])
        return row

    def candidates(
        self,
        scan_id: str,
        *,
        accepted_only: bool = False,
        selected_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be non-negative")
        sql = (
            "SELECT instrument_id, accepted, selected, score, data_timestamp, quality_status, payload "
            "FROM scanner_candidates WHERE scan_id = ?"
        )
        if accepted_only:
            sql += " AND accepted = 1"
        if selected_only:
            sql += " AND selected = 1"
        rows = self._db.query(
            sql + " ORDER BY selected DESC, accepted DESC, score DESC, instrument_id LIMIT ? OFFSET ?",
            (scan_id, limit, offset),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows


class CandidateResearchRepository(_Repository):
    """Persist source evidence separately from scanner and execution records."""

    def save_run(self, run: Any, snapshots: Iterable[Any]) -> None:
        snapshots = tuple(snapshots)
        with self._db.transaction():
            self._db.execute(
                """INSERT INTO research_runs
                   (run_id, scan_id, as_of, created_at, status, requested_count,
                    completed_count, failed_count, failure_summary, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, run.scan_id, ts(run.as_of), ts(run.created_at), run.status,
                 run.requested_count, run.completed_count, run.failed_count,
                 to_json(run.failure_summary), to_json(run)),
            )
            for snapshot in snapshots:
                self._db.execute(
                    """INSERT INTO research_snapshots
                       (snapshot_id, run_id, instrument_id, scanner_rank, scanner_score,
                        as_of, status, payload)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot.snapshot_id, snapshot.run_id, snapshot.instrument_id,
                     snapshot.scanner_rank, snapshot.scanner_score, ts(snapshot.as_of),
                     snapshot.status, to_json(snapshot)),
                )
                for item in snapshot.evidence:
                    self._db.execute(
                        """INSERT INTO research_evidence
                           (evidence_id, snapshot_id, component, observed_at,
                            retrieved_at, source, quality, payload)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (item.evidence_id, snapshot.snapshot_id, item.component,
                         ts(item.observed_at), ts(item.retrieved_at), item.source,
                         item.quality.value, to_json(item)),
                    )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM research_runs WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        for name in ("failure_summary", "payload"):
            row[name] = json.loads(row[name])
        return row

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM research_snapshots WHERE snapshot_id = ?", (snapshot_id,))
        if not rows:
            return None
        row = rows[0]
        row["payload"] = json.loads(row["payload"])
        return row

    def snapshots(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT snapshot_id, instrument_id, scanner_rank, scanner_score, "
            "as_of, status, payload FROM research_snapshots "
            "WHERE run_id = ? ORDER BY scanner_rank",
            (run_id,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def evidence(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT evidence_id, component, observed_at, retrieved_at, source, "
            "quality, payload FROM research_evidence WHERE snapshot_id = ? "
            "ORDER BY component, observed_at",
            (snapshot_id,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def save_opportunity(self, opportunity: Any) -> None:
        assessment = opportunity.assessment
        with self._db.transaction():
            self._db.execute(
                """INSERT INTO ai_research_assessments
                   (assessment_id, snapshot_id, status, provider, model_version,
                    prompt_version, schema_version, assessed_at, strategy_valid,
                    recommended_strategy, error, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (assessment.assessment_id, assessment.snapshot_id,
                 assessment.status.value, assessment.provider,
                 assessment.model_version, assessment.prompt_version,
                 assessment.schema_version, ts(assessment.assessed_at),
                 int(assessment.recommended_strategy is not None),
                 assessment.recommended_strategy, assessment.error,
                 to_json(assessment)),
            )
            ranking_score = (
                opportunity.ranking.score if opportunity.ranking is not None else None)
            self._db.execute(
                """INSERT INTO research_opportunities
                   (opportunity_id, assessment_id, state, ranking_score,
                    lifecycle, created_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (opportunity.opportunity_id, assessment.assessment_id,
                 opportunity.state.value, ranking_score,
                 to_json(opportunity.lifecycle), ts(opportunity.created_at),
                 to_json(opportunity)),
            )

    def get_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM ai_research_assessments WHERE assessment_id = ?",
            (assessment_id,),
        )
        if not rows:
            return None
        row = rows[0]
        row["payload"] = json.loads(row["payload"])
        opportunities = self._db.query(
            """SELECT opportunity_id, state, ranking_score, lifecycle,
                      created_at, payload
               FROM research_opportunities WHERE assessment_id = ?""",
            (assessment_id,),
        )
        if opportunities:
            opportunity = opportunities[0]
            opportunity["lifecycle"] = json.loads(opportunity["lifecycle"])
            opportunity["payload"] = json.loads(opportunity["payload"])
            row["opportunity"] = opportunity
        return row

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM research_opportunities WHERE opportunity_id = ?",
            (opportunity_id,),
        )
        if not rows:
            return None
        row = rows[0]
        row["lifecycle"] = json.loads(row["lifecycle"])
        row["payload"] = json.loads(row["payload"])
        return row

    def list_opportunities(
        self, *, limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        rows = self._db.query(
            """SELECT opportunity_id, assessment_id, state, ranking_score,
                      lifecycle, created_at, payload
               FROM research_opportunities
               ORDER BY CASE WHEN ranking_score IS NULL THEN 1 ELSE 0 END,
                        ranking_score DESC, created_at DESC, opportunity_id
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        for row in rows:
            row["lifecycle"] = json.loads(row["lifecycle"])
            row["payload"] = json.loads(row["payload"])
        return rows


class ProposalSubmissionRepository(_Repository):
    """Durable, one-shot claim and outcome for an accepted research proposal."""

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT * FROM proposal_submissions WHERE proposal_id = ?",
            (proposal_id,))
        return rows[0] if rows else None

    def begin(
        self,
        *,
        proposal_id: str,
        client_order_id: str,
        operator: str,
        sizing_mode: str,
        quantity: int | None,
        proposal_as_of: datetime,
        generated_at: datetime,
        proposal_payload: Any,
    ) -> bool:
        with self._db.transaction():
            if self.get(proposal_id) is not None:
                return False
            now = ts(datetime.now(timezone.utc))
            self._db.execute(
                """INSERT INTO proposal_submissions
                   (proposal_id, client_order_id, state, operator, sizing_mode,
                    quantity, proposal_as_of, generated_at, proposal_payload,
                    error, created_at, updated_at)
                   VALUES (?, ?, 'SUBMITTING', ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (proposal_id, client_order_id, operator, sizing_mode, quantity,
                 ts(proposal_as_of), ts(generated_at), to_json(proposal_payload),
                 now, now),
            )
            return True

    def finish(
        self,
        proposal_id: str,
        *,
        state: str,
        quantity: int | None,
        error: str | None,
    ) -> None:
        with self._db.transaction():
            self._db.execute(
                """UPDATE proposal_submissions
                   SET state = ?, quantity = ?, error = ?, updated_at = ?
                   WHERE proposal_id = ?""",
                (state, quantity, error, ts(datetime.now(timezone.utc)),
                 proposal_id),
            )


class Store:
    """One handle to every repository; migrations must already have been applied."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.instruments = InstrumentRepository(db)
        self.market_data = MarketDataMetadataRepository(db)
        self.signals = SignalRepository(db)
        self.risk_decisions = RiskDecisionRepository(db)
        self.strategy_decisions = StrategyDecisionRepository(db)
        self.orders = OrderRepository(db)
        self.fills = FillRepository(db)
        self.positions = PositionRepository(db)
        self.trades = TradeRepository(db)
        self.pnl = PnLRepository(db)
        self.news = NewsRepository(db)
        self.system_events = SystemEventRepository(db)
        self.audit = AuditRepository(db)
        self.strategy_configs = StrategyConfigRepository(db)
        self.execution_records = ExecutionRecordRepository(db)
        self.order_audit = OrderAuditRepository(db)
        self.proposal_submissions = ProposalSubmissionRepository(db)
        self.scanner_runs = ScannerRunRepository(db)
        self.research_runs = CandidateResearchRepository(db)
