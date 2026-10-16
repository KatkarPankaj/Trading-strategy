"""Adaptive opportunity discovery: rotate long-HOLD instruments out of the research Top-N.

A long HOLD is a discovery trigger, never an entry trigger. Rotated instruments are only
*deprioritized* in the deterministic scanner ranking so the next accepted candidates are
researched; they stay in the universe and are used again if nothing else qualifies. Every
newly researched candidate still passes validated selection, the deterministic strategy,
RiskEngine and execution checks. Position/order monitoring is independent of this policy.
State is derived from persisted cycle records, so it is restart-safe without new storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

# Persisted cycle outcomes that mean "evaluated, no entry warranted" (HOLD/NO_TRADE abstentions).
_HOLD_RESEARCH_STATUSES = {"NO_TRADE": "HOLD_RESEARCH_NO_TRADE",
                           "NO_VALIDATED_STRATEGY": "HOLD_NO_VALIDATED_STRATEGY"}


@dataclass(frozen=True, slots=True)
class AdaptiveDiscoverySettings:
    hold_threshold_minutes: int = 30
    cooldown_minutes: int = 60
    max_rotated: int = 20
    history_cycles: int = 20

    def __post_init__(self) -> None:
        for name, low, high in (("hold_threshold_minutes", 5, 1440), ("cooldown_minutes", 5, 1440),
                                ("max_rotated", 1, 500), ("history_cycles", 2, 200)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")


def parse_discovery_settings(env: Mapping[str, str]) -> AdaptiveDiscoverySettings | None:
    """PAPER_DISCOVERY_ENABLED=true opts in; disabled keeps the existing Top-N behaviour."""
    enabled = (env.get("PAPER_DISCOVERY_ENABLED") or "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise ValueError("PAPER_DISCOVERY_ENABLED must be true or false")
    if enabled == "false":
        return None
    return AdaptiveDiscoverySettings(
        hold_threshold_minutes=int(env.get("PAPER_DISCOVERY_HOLD_MINUTES") or 30),
        cooldown_minutes=int(env.get("PAPER_DISCOVERY_COOLDOWN_MINUTES") or 60),
        max_rotated=int(env.get("PAPER_DISCOVERY_MAX_ROTATED") or 20),
        history_cycles=int(env.get("PAPER_DISCOVERY_HISTORY_CYCLES") or 20),
    )


def _hold_code(candidate: Mapping[str, Any]) -> str | None:
    payload = candidate.get("payload") or {}
    if candidate.get("status") == "NON_ACTIONABLE":
        return "HOLD_STRATEGY_NO_SIGNAL"
    if candidate.get("status") == "RESEARCH_REJECTED":
        return _HOLD_RESEARCH_STATUSES.get((payload.get("research_assessment") or {}).get("status"))
    return None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def protected_instruments(position_management: Mapping[str, Any]) -> set[str] | None:
    """Instruments with positions or orders under monitoring; None when monitoring was incomplete."""
    if position_management.get("incomplete") or position_management.get("error"):
        return None
    return {str(row["instrument_id"])
            for row in (*position_management.get("positions", []), *position_management.get("orders", []))
            if row.get("instrument_id")}


def plan_rotation(
    runs: Iterable[Mapping[str, Any]], *, protected: set[str] | None, now: datetime,
    settings: AdaptiveDiscoverySettings,
) -> dict[str, Any]:
    """Deterministic plan: which instruments to deprioritize this cycle, with reason codes."""
    at = now.isoformat()
    if protected is None:
        return {"deprioritized": [], "decisions": [], "at": at,
                "skipped": "DISCOVERY_SKIPPED_MONITORING_INCOMPLETE"}
    ordered = sorted(
        (run for run in runs if _time(run.get("as_of")) is not None and _time(run["as_of"]) < now),
        key=lambda run: (run["as_of"], run.get("run_id", "")))
    cooldown = timedelta(minutes=settings.cooldown_minutes)
    rotated_at: dict[str, datetime] = {}
    for run in ordered:
        for decision in ((run.get("payload") or {}).get("discovery") or {}).get("decisions", []):
            if decision.get("action") == "ROTATED" and (stamp := _time(decision.get("rotated_at"))):
                rotated_at[str(decision["instrument_id"])] = stamp
    streaks: dict[str, tuple[datetime, str] | None] = {}
    for run in ordered:
        observed = _time(run["as_of"])
        for candidate in run.get("candidates", []):
            instrument = str(candidate.get("candidate_id") or "")
            if not instrument:
                continue
            last_rotation = rotated_at.get(instrument)
            if last_rotation is not None and observed < last_rotation + cooldown:
                continue  # observations before a rotation expired never re-trigger it
            code = _hold_code(candidate)
            if code is None:
                streaks[instrument] = None  # any non-HOLD outcome (order, rejection, error) resets
            elif streaks.get(instrument) is None:
                streaks[instrument] = (observed, code)
            else:
                streaks[instrument] = (streaks[instrument][0], code)
    decisions: list[dict[str, Any]] = []
    deprioritized: list[str] = []
    for instrument, stamp in sorted(rotated_at.items()):
        if now < stamp + cooldown and instrument not in protected:
            deprioritized.append(instrument)
            decisions.append({"instrument_id": instrument, "action": "ROTATED",
                              "reason_code": "COOLDOWN_ACTIVE", "rotated_at": stamp.isoformat(), "at": at})
        elif now < stamp + cooldown:
            decisions.append({"instrument_id": instrument, "action": "RETAINED",
                              "reason_code": "RETAINED_ACTIVE_POSITION_OR_ORDER", "at": at})
    threshold = timedelta(minutes=settings.hold_threshold_minutes)
    due = sorted(((start, instrument, code) for instrument, streak in streaks.items()
                  if streak is not None and instrument not in deprioritized
                  for start, code in (streak,)), key=lambda item: (item[0], item[1]))
    for start, instrument, code in due:
        held = now - start
        base = {"instrument_id": instrument, "hold_since": start.isoformat(),
                "hold_minutes": round(held.total_seconds() / 60, 1), "hold_code": code, "at": at}
        if held < threshold:
            decisions.append({**base, "action": "RETAINED", "reason_code": "HOLD_BELOW_THRESHOLD"})
        elif instrument in protected:
            decisions.append({**base, "action": "RETAINED",
                              "reason_code": "RETAINED_ACTIVE_POSITION_OR_ORDER"})
        elif len(deprioritized) >= settings.max_rotated:
            decisions.append({**base, "action": "RETAINED", "reason_code": "ROTATION_CAPACITY_REACHED"})
        else:
            deprioritized.append(instrument)
            decisions.append({**base, "action": "ROTATED", "reason_code": "HOLD_THRESHOLD_EXCEEDED",
                              "rotated_at": at})
    return {"deprioritized": sorted(deprioritized), "decisions": decisions, "at": at,
            "settings": {"hold_threshold_minutes": settings.hold_threshold_minutes,
                         "cooldown_minutes": settings.cooldown_minutes,
                         "max_rotated": settings.max_rotated}}


def record_added(plan: dict[str, Any], runs: Iterable[Mapping[str, Any]],
                 researched: Iterable[Mapping[str, Any]], at: str) -> None:
    """Annotate the plan with candidates researched for the first time in the history window."""
    seen = {str(candidate.get("candidate_id")) for run in runs for candidate in run.get("candidates", [])}
    plan["decisions"].extend(
        {"instrument_id": str(item["instrument_id"]), "action": "ADDED",
         "reason_code": "NEW_SCANNER_CANDIDATE", "research_stage": item.get("stage"), "at": at}
        for item in researched
        if item.get("instrument_id") and str(item["instrument_id"]) not in seen)
