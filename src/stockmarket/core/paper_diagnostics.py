"""Read-only summary of the latest paper cycle: why it did or did not place paper orders.

Derived only from persisted cycle/candidate records; it never authorizes, retries or
refreshes anything. Missing evidence is reported as UNRECORDED, not inferred.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Mapping

_ORDER_SUBMITTED = {"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED"}


def _candidate_reason(payload: Mapping[str, Any]) -> str | None:
    """Stable rejection code for a candidate, or None when it reached a paper order."""
    trace = payload.get("decision_trace") or {}
    if trace.get("final_status") in {"FILLED", "SUBMITTED", "PENDING"}:
        return None
    if (payload.get("order") or {}).get("status") in _ORDER_SUBMITTED:
        return None
    assessment = payload.get("research_assessment") or {}
    signal = ((payload.get("signal") or {}).get("signal") or {})
    text = (trace.get("rejection_code") or assessment.get("status")
            or next(iter(signal.get("reasons") or []), None)
            or (payload.get("signal") or {}).get("reason")
            or payload.get("rejection_code") or payload.get("research_error")
            or payload.get("error") or (payload.get("risk") or {}).get("reason"))
    if not text:
        return "UNRECORDED"
    return str(text).split(":", 1)[0].strip() or "UNRECORDED"


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def summarize_cycle(run: Mapping[str, Any] | None, *, now: datetime) -> dict[str, Any] | None:
    if run is None:
        return None
    reasons: Counter[str] = Counter()
    selected = submitted = filled = 0
    policy_blocked = False
    data_timestamps: list[datetime] = []
    for candidate in run.get("candidates", []):
        payload = candidate.get("payload") or {}
        assessment = payload.get("research_assessment") or {}
        if assessment.get("status") == "NO_VALIDATED_STRATEGY":
            policy_blocked = True
        if candidate.get("status") != "RESEARCH_REJECTED":
            selected += 1
        provenance = (payload.get("signal") or {}).get("provenance") or {}
        if (stamp := _parse(provenance.get("data_timestamp"))) is not None:
            data_timestamps.append(stamp)
        order = payload.get("order") or {}
        if order.get("status") in _ORDER_SUBMITTED:
            submitted += 1
        if float(order.get("filled_quantity") or 0) > 0:
            filled += 1
        if (reason := _candidate_reason(payload)) is not None:
            reasons[reason] += 1
    latest_data = max(data_timestamps, default=None)
    if policy_blocked:
        eligibility = "BLOCKED_NO_VALIDATED_STRATEGY"
    elif selected:
        eligibility = "STRATEGY_SELECTED"
    elif run.get("candidates"):
        eligibility = "NO_CANDIDATE_SELECTED"
    else:
        eligibility = "NO_CANDIDATES"
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "stage": run.get("stage"),
        "as_of": run.get("as_of"),
        "last_cycle_at": run.get("updated_at"),
        "universe_id": run.get("universe_id"),
        "candidates": len(run.get("candidates", [])),
        "strategy_eligibility": eligibility,
        "strategy_selected_candidates": selected,
        "primary_rejection_reason": reasons.most_common(1)[0][0] if reasons else None,
        "rejection_reasons": dict(reasons.most_common()),
        "data_freshness": {
            "latest_bar_timestamp": latest_data.isoformat() if latest_data else None,
            "age_seconds": round((now - latest_data).total_seconds(), 1) if latest_data else None,
            "status": "EVALUATED" if latest_data else "NOT_EVALUATED_NO_STRATEGY_SIGNAL",
        },
        "orders_submitted": submitted,
        "orders_with_fills": filled,
    }


def paper_diagnostics(
    *, run: Mapping[str, Any] | None, market: str, loop_status: Mapping[str, Any] | None,
    ledger_orders: int, ledger_fills: int, now: datetime,
) -> dict[str, Any]:
    cycle = summarize_cycle(run, now=now)
    last = (loop_status or {}).get("last") or {}
    blocked = cycle is not None and cycle["strategy_eligibility"] == "BLOCKED_NO_VALIDATED_STRATEGY"
    return {
        "trading_mode": "PAPER",
        "market": market,
        "scheduler": {
            "state": (loop_status or {}).get("state", "DISABLED"),
            "interval_minutes": (loop_status or {}).get("interval_minutes"),
            "entries_enabled": (loop_status or {}).get("entries_enabled", False),
            "last_tick_action": last.get("action"),
            "last_tick_at": last.get("at"),
            "consecutive_failures": (loop_status or {}).get("consecutive_failures", 0),
        },
        "last_cycle": cycle,
        "ledger": {"orders": ledger_orders, "fills": ledger_fills},
        "automatic_entries": "BLOCKED_NO_VALIDATED_STRATEGY" if blocked else (
            "NO_CYCLE_RECORDED" if cycle is None else "SUBJECT_TO_PER_ORDER_CHECKS"),
        "detail": (
            "Automatic selection requires an ACTIVE_FOR_PAPER Strategy Lab version with qualified, "
            "unexpired holdout evidence for the instrument; none qualified, so automatic cycles "
            "abstain by design." if blocked else
            "Read-only summary of persisted records; not an order authorization."),
    }
