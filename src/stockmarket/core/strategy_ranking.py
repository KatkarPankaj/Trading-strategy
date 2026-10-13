"""Advisory, version-matched ranking evidence and prospective paper measurements."""

from __future__ import annotations

from datetime import datetime
from statistics import mean, stdev
from typing import Any, Callable, Mapping, Sequence

from .outcome_feedback import OutcomeFeedbackService
from .persistence import Store
from .persistence.repositories import ts
from .strategies.base import strategy_parameters_hash


class StrategyRankingService:
    def __init__(self, store: Store, feedback: OutcomeFeedbackService,
                 *, clock: Callable[[], datetime]) -> None:
        self.store, self.feedback, self.clock = store, feedback, clock

    def evidence(self, as_of: datetime, market: str, currency: str,
                 strategies: Mapping[str, Any]) -> Mapping[str, Any]:
        feedback = dict(self.feedback.research_evidence(as_of, market, currency, strategies))
        evaluated = []
        for name, strategy in strategies.items():
            if not name.startswith("lab_"):
                continue
            proposal = self.store.strategy_lab.get(name[4:])
            if proposal is None or proposal["specification"]["market"] != market \
                    or proposal["research_context"]["currency"] != currency \
                    or strategy_parameters_hash(strategy) != proposal["parameters_hash"]:
                continue
            for report in self.store.strategy_lab.evaluations(proposal["proposal_id"]):
                if datetime.fromisoformat(report["end"]) > as_of \
                        or datetime.fromisoformat(report["evaluated_at"]) > as_of:
                    continue
                result = report.get("holdout_result")
                returns = [t["net_pnl"] / (t["quantity"] * t["entry_price"])
                           for t in result["trades"]] if result else []
                evaluated.append({
                    "strategy_id": name, "strategy_version": proposal["strategy_version_id"],
                    "parameters_hash": proposal["parameters_hash"],
                    "timeframe": proposal["specification"]["timeframe"],
                    "instrument_id": proposal["instrument_id"], "market_regime": "UNKNOWN",
                    "evaluation_id": report["evaluation_id"], "source": report["evaluation_kind"],
                    "status": report["status"], "metrics": result["metrics"] if result else None,
                    "session_count": result["session_count"] if result else 0,
                    "trade_count": len(returns),
                    "conservative_net_return": mean(returns) - 1.96 * stdev(returns) / len(returns) ** .5
                        if len(returns) >= 30 else None,
                })
        feedback["evaluations"] = evaluated[:4]
        feedback["omitted_evaluations"] = max(0, len(evaluated) - 4)
        return feedback

    def rank(
        self, ai_order: Sequence[str], context: Any, *, assessment_id: str,
    ) -> Mapping[str, Any]:
        feedback = context.outcome_feedback or {}
        evidence = []
        scores: dict[str, float] = {}
        for name in ai_order:
            groups = [g for g in feedback.get("performance", [])
                      if g["strategy_id"] == name
                      and g["sample_status"] == "QUALIFIED_PAPER_SAMPLE"
                      and g["market_regime"] == "UNKNOWN"
                      and g.get("return_ci95_half_width") is not None]
            holdouts = [g for g in feedback.get("evaluations", [])
                        if g["strategy_id"] == name and g["instrument_id"] == context.instrument_id
                        and g["source"] == "POST_PROPOSAL_HOLDOUT"
                        and g["status"] == "HOLDOUT_REVIEW_REQUIRED"]
            if len(groups) == 1:
                group = groups[0]
                score = group["mean_return_on_entry_notional"] - group["return_ci95_half_width"]
                source, sample = "PAPER_OUTCOMES", group["trade_count"]
                identity = group["evidence_hash"]
            elif holdouts:
                group = holdouts[0]
                score = group["conservative_net_return"]
                source, sample = "POST_PROPOSAL_HOLDOUT", group["trade_count"]
                identity = group["evaluation_id"]
            else:
                score, source, sample, identity = 0.0, "AI_RESEARCH_ONLY", 0, None
            scores[name] = score
            evidence.append(dict(strategy=name, source=source, sample_size=sample,
                                 conservative_net_return=score, evidence_id=identity,
                                 reason="Cost-adjusted mean return minus approximate 95% uncertainty; "
                                        "UNKNOWN regime is not claimed regime-specific evidence. "
                                        "Insufficient samples do not change the AI preference."))
        # Unknown evidence is neutral, not a fabricated profitable baseline.
        ranked = sorted(ai_order, key=lambda name: -scores[name])
        record = dict(
            assessment_id=assessment_id, instrument_id=context.instrument_id,
            snapshot_id=context.snapshot_id, as_of=ts(context.as_of),
            recorded_at=ts(self.clock()), ai_order=list(ai_order), ranked_order=ranked,
            changed=list(ai_order) != ranked, evidence=evidence,
            baseline="AI ordering without the deterministic feedback adjustment; NO_TRADE return = 0",
            policy="advisory-conservative-return-v1; no risk/sizing changes; not causal proof of improvement",
        )
        self.store.strategy_lab.save_ranking(record, self.clock())
        return record

    def measurements(self) -> Mapping[str, Any]:
        as_of = self.clock()
        outcomes = {o["fill_ids"][0]: o for o in self.store.paper_outcomes.available(as_of)}
        fills = {f["fill_id"]: f for f in self.store.fills.all()}
        rows = []
        for ranking in self.store.strategy_lab.rankings(1000):
            if not ranking["ranked_order"]:
                continue
            matches = []
            for fill_id, outcome in outcomes.items():
                fill = fills.get(fill_id)
                execution = self.store.execution_records.for_order(fill["client_order_id"]) if fill else None
                generation = self.store.signal_generations.get_by_signal(
                    str(execution["signal_id"])) if execution and execution["signal_id"] else None
                opportunity = self.store.research_runs.get_opportunity(
                    generation["opportunity_id"]) if generation else None
                if opportunity and opportunity["assessment_id"] == ranking["assessment_id"] \
                        and datetime.fromisoformat(outcome["opened_at"]) >= datetime.fromisoformat(ranking["recorded_at"]):
                    matches.append(outcome)
            selected = ranking["ranked_order"][0]
            source = next(e["source"] for e in ranking["evidence"] if e["strategy"] == selected)
            rows.append({**ranking, "selected_source": source,
                         "completed_forward_trades": len(matches),
                         "forward_net_pnl": sum(o["net_pnl"] for o in matches),
                         "forward_outcome_ids": [o["outcome_id"] for o in matches]})
        return {"as_of": ts(as_of), "rankings": rows,
                "improvement_status": "NOT_ESTABLISHED",
                "limitation": "Prospective executed-paper outcomes only; unexecuted alternatives have no "
                              "counterfactual fills. No matched randomized/control cohort or sufficient "
                              "independent samples yet; prompt delivery is not proof of learning."}
