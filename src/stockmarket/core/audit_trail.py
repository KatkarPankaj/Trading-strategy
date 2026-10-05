"""Audit trail: what must be captured for every order, and how to rebuild the full decision from storage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Decision inputs supplied by the caller (strategy/signal layer); the service adds market data, risk and broker data."""

    technical_signals: Mapping[str, Any] = field(default_factory=dict)
    news_signals: Mapping[str, Any] = field(default_factory=dict)
    ai_analysis_ids: tuple[str, ...] = ()
    strategy_decision_id: str | None = None
    sizing: Mapping[str, Any] = field(default_factory=dict)
    data_reference: str | None = None
    exit_reason: str | None = None


def live_gaps(audit: AuditContext | None, *, is_exit: bool) -> list[str]:
    """Live orders must be reconstructable: entries need signals and sizing, exits need a reason."""
    if is_exit:
        return [] if audit is not None and audit.exit_reason else ["exit_reason"]
    gaps = []
    if audit is None or not audit.technical_signals:
        gaps.append("technical_signals")
    if audit is None or not audit.sizing:
        gaps.append("sizing")
    return gaps


def _loads(value: str | None) -> Any:
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def reconstruct(store: Any, client_order_id: str) -> dict[str, Any] | None:
    """Everything known about one order, plus a completeness map showing which sections are present."""
    order = store.orders.get(client_order_id)
    if order is None:
        return None
    events = store.orders.events(client_order_id)
    audit = store.order_audit.get(client_order_id) or {}
    execution = store.execution_records.for_order(client_order_id)
    risk = store.risk_decisions.get(
        order["risk_decision_id"]) if order["risk_decision_id"] else None
    signal = store.signals.get(
        order["signal_id"]) if order["signal_id"] else None
    decision_id = audit.get("strategy_decision_id")
    decision = store.strategy_decisions.get(
        decision_id) if decision_id else None
    if decision is not None:
        decision = {**decision, "explanation": _loads(decision["explanation"])}
    ai = store.news.analyses_by_id(audit.get("ai_analysis_ids") or [])
    fills = [{"timestamp": e["timestamp"], "quantity": e["fill_quantity"], "price": e["fill_price"]}
             for e in events if e.get("fill_quantity")]
    is_exit = bool(audit.get("exit_reason"))
    present = {
        "market_data": bool(audit.get("market_data")),
        "technical_signals": bool(audit.get("technical_signals")),
        "news_signals": bool(audit.get("news_signals")),
        "ai_analysis": bool(ai),
        "strategy_decision": decision is not None,
        "risk_decision": risk is not None,
        "position_sizing": bool(audit.get("sizing")),
        "final_order": True,
        "broker_response": bool(audit.get("broker_response")),
        "fills": bool(fills),
        "execution_record": execution is not None,
        "exit_reason": is_exit,
    }
    return {
        "client_order_id": client_order_id,
        "order": order,
        "events": events,
        "execution_record": execution,
        "signal": signal,
        "strategy_decision": decision,
        "market_data": audit.get("market_data"),
        "data_reference": audit.get("data_reference"),
        "technical_signals": audit.get("technical_signals"),
        "news_signals": audit.get("news_signals"),
        "ai_analyses": ai,
        "risk_decision": risk,
        "risk_inputs": audit.get("risk_inputs"),
        "position_sizing": audit.get("sizing"),
        "broker_response": audit.get("broker_response"),
        "fills": fills,
        "exit_reason": audit.get("exit_reason"),
        "completeness": present,
    }
