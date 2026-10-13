"""Advisory, version-matched ranking evidence and prospective paper measurements."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
from typing import Any, Callable, Mapping, Sequence

from .outcome_feedback import OutcomeFeedbackService
from .persistence import Store
from .persistence.repositories import ts
from .strategies.base import strategy_parameters_hash
from .backtesting.evidence import summarize_evidence
from .persistence.repositories import to_json

logger = logging.getLogger(__name__)


class StrategyRankingService:
    def __init__(self, store: Store, feedback: OutcomeFeedbackService,
                 *, clock: Callable[[], datetime]) -> None:
        self.store, self.feedback, self.clock = store, feedback, clock
        self.strategy_source: Callable[[], Mapping[str, Any]] | None = None

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
                # Final holdouts are reserved for independent review, never future selection or tuning.
                if report.get("evaluation_kind") != "HISTORICAL":
                    continue
                if datetime.fromisoformat(report["end"]) > as_of \
                        or datetime.fromisoformat(report["evaluated_at"]) > as_of:
                    continue
                evaluated.append({
                    "strategy_id": name, "strategy_version": proposal["strategy_version_id"],
                    "parameters_hash": proposal["parameters_hash"],
                    "timeframe": proposal["specification"]["timeframe"],
                    "instrument_id": proposal["instrument_id"], "market_regime": "UNKNOWN",
                    "evaluation_id": report["evaluation_id"], "source": report["evaluation_kind"],
                    "status": "EXPLORATORY_NOT_RANKING_EVIDENCE",
                    "validation_windows": [
                        {"window": fold["window"]["validate"], "metrics": fold["validate"]["metrics"]}
                        for fold in report["folds"]],
                })
        feedback["evaluations"] = evaluated[:4]
        feedback["omitted_evaluations"] = max(0, len(evaluated) - 4)
        return feedback

    def rank(
        self, ai_order: Sequence[str], context: Any, *, assessment_id: str,
    ) -> Mapping[str, Any]:
        now = self.clock()
        if context.as_of > now:
            raise ValueError("strategy ranking cannot use a future decision timestamp")
        if not ai_order or len(set(ai_order)) != len(ai_order):
            raise ValueError("strategy ordering must be nonempty and unique")
        if self.strategy_source is None:
            raise ValueError("strategy ranking requires a trusted current catalog")
        strategies = self.strategy_source()
        if any(name not in strategies for name in ai_order):
            raise ValueError("strategy ranking contains an unregistered strategy")
        evidence = []
        scores: dict[str, float] = {}
        comparisons = len(strategies)
        for name in ai_order:
            observations = self.feedback.observations(
                context.as_of, strategy_id=name, strategy=strategies[name],
                instrument_id=context.instrument_id, market=context.market, currency=context.currency,
                regime="UNKNOWN")
            error = None
            try:
                summary = summarize_evidence(observations, as_of=context.as_of, comparisons=comparisons)
            except ValueError as exc:
                # Persist the rejection, not a plausible zero-performance replacement.
                error = str(exc)
                logger.warning("strategy ranking evidence rejected", extra={
                    "strategy": name, "instrument_id": context.instrument_id, "reason": error})
                summary = {"sample_status": "INVALID_EVIDENCE", "score": None,
                           "trade_count": len(observations)}
            score = summary["score"]
            if score is not None:
                scores[name] = score
            source = "INVALID_OUTCOME_EVIDENCE" if error else \
                "PAPER_OUTCOMES" if score is not None else "AI_RESEARCH_ONLY"
            identity = hashlib.sha256(to_json(observations).encode()).hexdigest()
            evidence.append(dict(strategy=name, source=source, sample_size=len(observations),
                                 strategy_version=getattr(strategies[name], "version", None),
                                 parameters_hash=strategy_parameters_hash(strategies[name]),
                                 timeframe=getattr(getattr(strategies[name], "config", None), "interval", None),
                                 outcome_ids=[o.identity for o in observations],
                                 conservative_net_return=score, evidence_id=identity,
                                 metrics=summary, rejection_reason=error,
                                 market_regime="UNKNOWN", regime_status="NOT_CONDITIONED",
                                 reason="Version/instrument/timeframe-matched net returns; "
                                        "chronological stability, worst-window/drawdown penalties and "
                                        "catalog-multiplicity-adjusted uncertainty. "
                                        "Final holdouts excluded. Insufficient evidence is not poor performance."))
        # Compare only like-qualified samples: never rank unknown performance as zero.
        qualified = sorted(scores, key=lambda name: -scores[name])
        queue = iter(qualified)
        ranked = [next(queue) if name in scores else name for name in ai_order]
        record = dict(
            assessment_id=assessment_id, instrument_id=context.instrument_id,
            snapshot_id=context.snapshot_id, as_of=ts(context.as_of),
            recorded_at=ts(now), ai_order=list(ai_order), ranked_order=ranked,
            changed=list(ai_order) != ranked, evidence=evidence,
            recommendation="NO_TRADE" if len(scores) == len(ai_order)
                and max(scores.values()) <= 0 else "RESEARCH_SELECTION",
            abstention_reason="All compared sample-qualified strategies are below the NO_TRADE baseline."
                if len(scores) == len(ai_order) and max(scores.values()) <= 0 else None,
            catalog_size=comparisons, compared_strategies=list(ai_order),
            coverage="Only AI-recommended alternatives are eligible for this advisory reordering.",
            baseline="AI ordering without the deterministic feedback adjustment; NO_TRADE return = 0",
            policy="advisory-evidence-v2; >=30 trades/10 observed days/3 chronological blocks; "
                   "qualified slots only; final holdouts excluded; no risk/sizing changes",
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
