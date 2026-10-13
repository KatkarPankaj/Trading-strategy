"""Closed position episodes and advisory, time-safe paper performance evidence."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from math import isclose, isfinite, sqrt
from statistics import mean, stdev
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .persistence import Store
from .persistence.repositories import ts
from .strategies.base import strategy_parameters_hash


class OutcomeFeedbackService:
    def __init__(self, store: Store, *, clock: Callable[[], datetime]) -> None:
        self.store = store
        self.clock = clock

    def process(self) -> int:
        """Replay durable fills; emit only fully closed episodes, never partial exits."""
        known_at = self.clock()
        ts(known_at)
        states: dict[str, dict[str, Any]] = {}
        created = 0
        with self.store.db.transaction():
            for fill in self.store.paper_outcomes.ordered_fills():
                at = datetime.fromisoformat(fill["timestamp"])
                ts(at)
                if at > known_at:
                    raise ValueError("paper fill is in the future")
                q, price = fill["quantity"], fill["price"]
                if isinstance(q, bool) or not isinstance(q, int) or q <= 0 \
                        or any(not isfinite(fill[k]) for k in
                               ("price", "fee", "slippage", "realized_pnl")) \
                        or price <= 0 or fill["fee"] < 0 or fill["slippage"] < 0 \
                        or fill["side"] not in {"BUY", "SELL"}:
                    raise ValueError("invalid persisted fill for outcome processing")
                signed = q if fill["side"] == "BUY" else -q
                iid = fill["instrument_id"]
                state = states.get(iid)
                if state is not None and state["currency"] != fill["currency"]:
                    raise ValueError("episode currency changed between fills")
                if state is not None and at < datetime.fromisoformat(state["last_at"]):
                    raise ValueError("out-of-order paper fills require reconciliation")
                closed = min(abs(state["position"]), q) if state \
                    and state["position"] * signed < 0 else 0
                expected = closed * (price - state["average"]) * state["direction"] \
                    if closed and state else 0.0
                if not isclose(expected, fill["realized_pnl"], abs_tol=1e-7, rel_tol=1e-9):
                    raise ValueError("paper fill realized PnL disagrees with episode accounting")
                if closed and state:
                    fraction = closed / q
                    state["exit_quantity"] += closed
                    state["exit_notional"] += closed * price
                    state["gross_pnl"] += expected
                    state["fees"] += fill["fee"] * fraction
                    state["slippage"] += fill["slippage"] * fraction
                    state["fill_ids"].append(fill["fill_id"])
                    audit = self.store.order_audit.get(fill["client_order_id"]) \
                        if fill["client_order_id"] else None
                    state["exit_reasons"].append((audit or {}).get("exit_reason") or "UNKNOWN")
                    state["position"] += closed * (1 if signed > 0 else -1)
                    state["last_at"] = fill["timestamp"]
                    if state["position"] == 0:
                        outcome = {k: v for k, v in state.items()
                                   if k not in {"position", "average", "direction", "last_at"}}
                        outcome.update(
                            closed_at=fill["timestamp"],
                            holding_seconds=(at - datetime.fromisoformat(state["opened_at"])).total_seconds(),
                            net_pnl=state["gross_pnl"] - state["fees"],
                            entry_price=state["entry_notional"] / state["entry_quantity"],
                            exit_price=state["exit_notional"] / state["exit_quantity"],
                        )
                        if self.store.paper_outcomes.save(outcome, known_at):
                            created += 1
                            self.store.audit.append(
                                "outcome_feedback", "PAPER_OUTCOME_RECORDED",
                                "paper_outcome", outcome["outcome_id"],
                                {"instrument_id": iid, "currency": outcome["currency"],
                                 "gross_pnl": outcome["gross_pnl"], "fees": outcome["fees"],
                                 "net_pnl": outcome["net_pnl"], "attribution": outcome["attribution"]},
                                timestamp=known_at)
                        del states[iid]
                        state = None
                remaining = q - closed
                if remaining:
                    if state is None:
                        state = self._episode(fill, signed)
                        states[iid] = state
                    else:
                        attribution = self._attribution(fill)
                        if attribution != state["attribution"]:
                            state["attribution_status"] = "MIXED"
                    total = abs(state["position"]) + remaining
                    state["average"] = (abs(state["position"]) * state["average"]
                                        + remaining * price) / total
                    state["position"] += remaining * (1 if signed > 0 else -1)
                    state["entry_quantity"] += remaining
                    state["entry_notional"] += remaining * price
                    state["fees"] += fill["fee"] * remaining / q
                    state["slippage"] += fill["slippage"] * remaining / q
                    state["fill_ids"].append(fill["fill_id"])
                    state["last_at"] = fill["timestamp"]
        return created

    def _attribution(self, fill: Mapping[str, Any]) -> dict[str, Any]:
        oid = fill["client_order_id"]
        record = self.store.execution_records.for_order(oid) if oid else None
        return {
            "strategy_id": (record or {}).get("strategy_name"),
            "strategy_version": (record or {}).get("strategy_version"),
            "parameters_hash": (record or {}).get("parameters_hash"),
            "timeframe": (record or {}).get("timeframe"),
            "market_regime": (record or {}).get("market_regime"),
        }

    def _episode(self, fill: Mapping[str, Any], signed: int) -> dict[str, Any]:
        attribution = self._attribution(fill)
        audit = self.store.order_audit.get(fill["client_order_id"]) \
            if fill["client_order_id"] else None
        instrument = self.store.instruments.get(fill["instrument_id"])
        return {
            "outcome_id": str(uuid5(NAMESPACE_URL, "paper-episode:" + fill["fill_id"])),
            "instrument_id": fill["instrument_id"],
            "market": (instrument or {}).get("market"),
            "currency": fill["currency"], "side": "LONG" if signed > 0 else "SHORT",
            "opened_at": fill["timestamp"], "last_at": fill["timestamp"],
            "attribution": attribution,
            "attribution_status": "COMPLETE" if all(attribution.values())
                and attribution["strategy_version"] != "UNVERSIONED" else "INCOMPLETE",
            "entry_reasons": ((audit or {}).get("technical_signals") or {}).get("reasons", []),
            "exit_reasons": [], "fill_ids": [], "position": 0,
            "direction": 1 if signed > 0 else -1, "average": 0.0,
            "entry_quantity": 0, "exit_quantity": 0,
            "entry_notional": 0.0, "exit_notional": 0.0,
            "gross_pnl": 0.0, "fees": 0.0, "slippage": 0.0,
        }

    def performance(self, as_of: datetime) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for outcome in self.store.paper_outcomes.available(as_of):
            key = json.dumps([outcome["market"], outcome["currency"], outcome["attribution"],
                              outcome["attribution_status"]], sort_keys=True)
            groups[key].append(outcome)
        results = []
        for key, outcomes in sorted(groups.items()):
            market, currency, attribution, status = json.loads(key)
            pnls = [o["net_pnl"] for o in outcomes]
            returns = [o["net_pnl"] / o["entry_notional"] for o in outcomes]
            count = len(pnls)
            wins = sum(p for p in pnls if p > 0)
            losses = -sum(p for p in pnls if p < 0)
            cumulative = peak = drawdown = 0.0
            for pnl in pnls:
                cumulative += pnl
                peak = max(peak, cumulative)
                drawdown = max(drawdown, peak - cumulative)
            uncertainty = 1.96 * stdev(pnls) / sqrt(count) if count > 1 else None
            results.append({
                **attribution, "market": market, "currency": currency,
                "attribution_status": status, "trade_count": count,
                "sample_status": "QUALIFIED_PAPER_SAMPLE" if count >= 30 and status == "COMPLETE"
                    else "INSUFFICIENT_EVIDENCE",
                "win_rate": sum(p > 0 for p in pnls) / count,
                "expectancy": mean(pnls), "gross_pnl": sum(o["gross_pnl"] for o in outcomes),
                "fees": sum(o["fees"] for o in outcomes), "net_pnl": sum(pnls),
                "profit_factor": wins / losses if losses else None,
                "profit_factor_status": "DEFINED" if losses else "NO_LOSSES",
                "max_closed_pnl_drawdown": drawdown,
                "expectancy_ci95_half_width": uncertainty,
                "mean_return_on_entry_notional": mean(returns),
                "return_volatility": stdev(returns) if count > 1 else None,
                "return_ci95_half_width": 1.96 * stdev(returns) / sqrt(count) if count > 1 else None,
                "return_mean_to_volatility": mean(returns) / stdev(returns)
                    if count > 1 and stdev(returns) > 0 else None,
                "evidence_hash": hashlib.sha256(
                    json.dumps([o["outcome_id"] for o in outcomes]).encode()).hexdigest(),
                "last_known_at": max(o["known_at"] for o in outcomes),
                "out_of_sample_validated": False,
            })
        return results

    def research_evidence(
        self, as_of: datetime, market: str, currency: str,
        strategies: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        compatible = [p for p in self.performance(as_of)
                      if p["market"] == market and p["currency"] == currency
                      and p["strategy_id"] in strategies
                      and p["attribution_status"] == "COMPLETE"
                      and p["strategy_version"] == getattr(strategies[p["strategy_id"]], "version", None)
                      and p["parameters_hash"] == strategy_parameters_hash(strategies[p["strategy_id"]])]
        context_fields = {
            "strategy_id", "strategy_version", "parameters_hash", "timeframe", "market_regime",
            "trade_count", "sample_status", "win_rate", "expectancy", "profit_factor",
            "max_closed_pnl_drawdown", "expectancy_ci95_half_width", "return_mean_to_volatility",
            "evidence_hash", "last_known_at", "out_of_sample_validated",
            "mean_return_on_entry_notional", "return_volatility", "return_ci95_half_width",
        }
        return {
            "as_of": ts(as_of), "source": "persisted_closed_paper_episodes",
            "policy": "Advisory only; insufficient samples and paper results do not authorize trades or risk increases. Not out-of-sample validation.",
            "performance": [{k: v for k, v in group.items() if k in context_fields}
                            for group in compatible[:4]],
            "omitted_groups": max(0, len(compatible) - 4),
        }
