"""Frozen, retrospective comparisons. Never tune against a registered final holdout."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
from statistics import mean, stdev
from math import sqrt
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .ai.schemas import StrategyProposalSchema
from .backtesting.engine import BacktestResult
from .backtesting.evidence import PerformanceObservation, summarize_evidence
from .backtesting.strategy_adapter import strategy_signal_fn
from .backtesting.walk_forward import make_windows
from .data.provider import interval_delta
from .persistence.repositories import to_json, ts
from .strategies.orb_vwap import OrbVwapConfig, OrbVwapStrategy
from .strategies.base import strategy_parameters, strategy_parameters_hash, Strategy
from .strategy_lab import StrategyLabError, StrategyLabService


class StrategyComparisonService:
    def __init__(self, laboratory: StrategyLabService) -> None:
        self.lab = laboratory

    def compare(self, proposal_ids: list[str], *, start: datetime, end: datetime) -> dict[str, Any]:
        if not 1 <= len(proposal_ids) <= 8 or len(set(proposal_ids)) != len(proposal_ids):
            raise StrategyLabError("comparison requires one to eight unique strategy versions")
        now = self.lab.clock()
        if any(t.tzinfo is None or t.utcoffset() is None for t in (start, end)) \
                or start >= end or end > now or end - start > timedelta(days=180):
            raise StrategyLabError("comparison requires a completed aware historical window")
        frozen = []
        for pid in sorted(proposal_ids):
            proposal = self.lab.store.strategy_lab.get(pid)
            if proposal is None:
                raise StrategyLabError("unknown strategy proposal")
            frozen.append(proposal)
        instrument_ids = {p["instrument_id"] for p in frozen}
        if len(instrument_ids) != 1:
            raise StrategyLabError("matched comparison requires the same instrument")
        specs = [StrategyProposalSchema.model_validate(p["specification"]) for p in frozen]
        if len({s.market for s in specs}) != 1 or len({s.timeframe for s in specs}) != 1:
            raise StrategyLabError("matched comparison requires the same market and timeframe")
        instrument = self.lab.instruments.get(next(iter(instrument_ids)))
        session = self.lab.sessions.get(specs[0].market)
        if instrument is None or session is None:
            raise StrategyLabError("instrument/session metadata unavailable")
        if not instrument.tradable or any(s.outcome != "PROPOSAL" for s in specs) \
                or any(s.parameters.allow_short and instrument.shortable is not True for s in specs):
            raise StrategyLabError("proposal/instrument trading eligibility is unavailable")
        market = self.lab.assessment.markets.get(instrument.market)
        engine = self.lab.backtest_engine(
            instrument, specs[0].timeframe, allow_short=any(s.parameters.allow_short for s in specs))
        settings = self.lab._evaluation_policy(frozen[0], specs[0], start, end, .001, 5)
        interval = specs[0].timeframe
        baseline = OrbVwapStrategy(OrbVwapConfig(interval=interval))
        strategies: dict[str, Strategy] = {"fixed-orb": baseline}
        strategies.update({p["proposal_id"]: self.lab._strategy(s) for p, s in zip(frozen, specs)})
        if any(p["parameters_hash"] != strategy_parameters_hash(strategies[p["proposal_id"]])
               or p["template_version"] != strategies[p["proposal_id"]].version for p in frozen):
            raise StrategyLabError("persisted strategy template/configuration mismatch")
        policy = {
            "version": "matched-exploratory-v1", "proposal_ids": sorted(proposal_ids),
            "start": ts(start), "end": ts(end), "instrument_id": instrument.instrument_id,
            "timeframe": interval, "evaluation_policy": settings,
            "baseline": {"version": baseline.version, "parameters": strategy_parameters(baseline)},
            "candidate_configurations": {p["proposal_id"]: p["parameters_hash"] for p in frozen},
            "windows": {"train": 20, "validate": 10, "test": 10, "step": 10, "embargo": 1},
            "scoring_policy": "advisory-evidence-v2",
        }
        comparison_id = hashlib.sha256(to_json(policy).encode()).hexdigest()
        existing = self.lab.store.strategy_lab.comparison(comparison_id)
        if existing is not None:
            return existing
        for reservation in self.lab.store.strategy_lab.reserved_windows(instrument.instrument_id):
            if start <= datetime.fromisoformat(reservation["end"]) \
                    and end >= datetime.fromisoformat(reservation["start"]):
                raise StrategyLabError("comparison overlaps a reserved final holdout")
        data = self.lab.historical_bars(instrument, interval, start, end)
        data = data.loc[:, ["open", "high", "low", "close", "volume"]].astype(float)
        data.index = data.index.tz_convert("UTC").rename("bar_start")
        data_hash = hashlib.sha256(data.to_csv().encode()).hexdigest()
        calendar = market.calendar
        zone = ZoneInfo(instrument.timezone)
        step = interval_delta(interval)
        if data.index[-1].to_pydatetime() + step > now:
            raise StrategyLabError("comparison contains incomplete bars")
        # Observed dates alone cannot prove that whole historical sessions are present.
        days = pd.date_range(start.astimezone(zone).date(), end.astimezone(zone).date(), freq="D")
        if any(not market.is_covered(day.date()) for day in days):
            raise StrategyLabError("comparison exchange calendar coverage unavailable")
        sessions = [day.date() for day in days if calendar.is_trading_day(day.date())]

        def bounds(day: date) -> tuple[datetime, datetime]:
            return (datetime.combine(day, calendar.open_time, zone),
                    datetime.combine(day, calendar.close_time_on(day), zone))
        dates = []
        for day in sessions:
            opening, closing = bounds(day)
            if opening < start or closing > end:
                raise StrategyLabError("comparison range must contain complete exchange sessions")
            expected = pd.date_range(opening, closing, freq=step, inclusive="left")
            expected = expected[[calendar.is_open(t) for t in expected]]
            available = data.loc[(data.index >= opening) & (data.index < closing)]
            if len(available) != len(expected) or not expected.isin(available.index).all():
                raise StrategyLabError(f"comparison missing historical session bars: {day}")
            dates.append(day)
        windows = make_windows(dates, train=20, validate=10, test=10, step=10, embargo=1)
        if not windows:
            raise StrategyLabError("comparison requires at least 42 complete exchange sessions")
        chosen_observations = []
        fixed_observations = []
        paired = []
        folds = []
        max_drawdown = {"fixed_strategy": 0.0, "ranking_only": 0.0}
        for index, window in enumerate(windows):
            decision_at = bounds(window.test[0])[0]
            # Validation ends before the embargo and test. Test metrics are never read here.
            validation = {}
            for name, strategy in strategies.items():
                result = engine.run(
                    {instrument.instrument_id: instrument}, {instrument.instrument_id: data},
                    strategy_signal_fn(strategy, instrument, session),
                    calendars={instrument.instrument_id: calendar},
                    start=window.validate[0], end=window.validate[1])
                observations = self._observations(result, f"{index}:{name}:validate")
                validation[name] = summarize_evidence(
                    observations, as_of=decision_at, comparisons=len(strategies))
            qualified = [(name, v["score"]) for name, v in validation.items()
                         if v["score"] is not None and v["score"] > 0]
            selected = max(qualified, key=lambda item: (item[1], item[0]))[0] if qualified else None
            test_results = {}
            for name in dict.fromkeys(["fixed-orb"] + ([selected] if selected else [])):
                result = engine.run(
                    {instrument.instrument_id: instrument}, {instrument.instrument_id: data},
                    strategy_signal_fn(strategies[name], instrument, session),
                    calendars={instrument.instrument_id: calendar},
                    start=window.test[0], end=window.test[1])
                test_results[name] = result
            fixed = test_results["fixed-orb"]
            selected_result = test_results.get(selected)
            fixed_obs = self._observations(fixed, f"{index}:fixed:test")
            selected_obs = self._observations(selected_result, f"{index}:selected:test") \
                if selected_result else []
            fixed_observations.extend(fixed_obs)
            chosen_observations.extend(selected_obs)
            fixed_return = fixed.metrics["total_return"]
            selected_return = selected_result.metrics["total_return"] if selected_result else 0.0
            paired.append(selected_return - fixed_return)
            max_drawdown["fixed_strategy"] = max(max_drawdown["fixed_strategy"], fixed.metrics["max_drawdown"])
            if selected_result:
                max_drawdown["ranking_only"] = max(max_drawdown["ranking_only"], selected_result.metrics["max_drawdown"])
            folds.append({
                "window": asdict(window), "decision_at": decision_at.isoformat(),
                "selection_evidence": validation, "selected_strategy": selected or "NO_TRADE",
                "selection_reason": "Highest positive qualified validation score; final/test results excluded."
                    if selected else "No validation sample meets the evidence policy.",
                "fixed_metrics": fixed.metrics,
                "ranking_metrics": selected_result.metrics if selected_result else None,
                "fixed_skips": fixed.skipped,
                "ranking_skips": selected_result.skipped if selected_result else {},
                "fixed_net_return": fixed_return, "ranking_net_return": selected_return,
                "paired_net_return_difference": paired[-1],
            })
        arms = {}
        for name, observations in (("fixed_strategy", fixed_observations), ("ranking_only", chosen_observations)):
            summary = summarize_evidence(observations, as_of=now, comparisons=len(strategies))
            summary.update({
                "status": "EVALUATED", "max_window_equity_drawdown": max_drawdown[name],
                "mean_window_net_return": mean(f["fixed_net_return" if name == "fixed_strategy"
                                                 else "ranking_net_return"] for f in folds),
                "entry_turnover": sum(o.entry_notional for o in observations) / engine.config.starting_cash,
                "capital_convention": "Each nonoverlapping test window resets the same initial capital.",
            })
            arms[name] = summary
        arms["learning_model_enhanced"] = {
            "status": "UNAVAILABLE",
            "reason": "No versioned point-in-time feature/label dataset or fitted supervised model is registered."}
        arms["regime_conditioned_selection"] = {
            "status": "UNAVAILABLE",
            "reason": "No timestamped categorical regime series is registered for these historical bars."}
        report = {
            "comparison_id": comparison_id, "created_at": now.isoformat(), "policy": policy,
            "data_hash": data_hash, "provider": self.lab.market_data.name,
            "evidence_status": "RETROSPECTIVE_EXPLORATORY",
            "improvement_status": "NOT_ESTABLISHED",
            "arms": arms, "folds": folds, "paired_comparison": {
                "window_count": len(paired), "mean_net_return_difference": mean(paired),
                "ci95_half_width": 1.96 * stdev(paired) / sqrt(len(paired)) if len(paired) >= 3 else None,
                "uncertainty_status": "APPROXIMATE_WINDOW_INTERVAL" if len(paired) >= 3 else "INSUFFICIENT_WINDOWS",
            },
            "test_observations": {
                "fixed_strategy": fixed_observations, "ranking_only": chosen_observations},
            "limitations": [
                "Candidates may have been designed using this historical data; this is not final out-of-sample proof.",
                "Train periods are reserved; ranking fits validation only; no learning model is fitted.",
                "Only closed test trades count; cash-reset windows are not a continuous portfolio return.",
                "Spread/slippage are embedded in fills, commissions are explicit fees; never deducted twice.",
                "Improvement requires a preregistered prospective matched experiment, not this retrospective report.",
            ],
        }
        snapshot = {
            "data_hash": data_hash,
            "columns": list(data.columns), "timestamps": [t.isoformat() for t in data.index],
            "timezone": str(data.index.tz), "index_name": data.index.name,
            "bars": data.values.tolist(), "serialization": "canonical-float64-utc-csv-sha256",
        }
        with self.lab.store.db.transaction():
            concurrent = self.lab.store.strategy_lab.comparison(comparison_id)
            if concurrent is not None:
                return concurrent
            self.lab.store.strategy_lab.save_comparison_data(snapshot, now)
            self.lab.store.strategy_lab.save_comparison(report, now)
        persisted = self.lab.store.strategy_lab.comparison(comparison_id)
        if persisted is None:
            raise RuntimeError("persisted comparison is unavailable")
        return persisted

    @staticmethod
    def _observations(result: BacktestResult, prefix: str) -> list[PerformanceObservation]:
        episodes: dict[tuple, list] = {}
        for trade in result.trades:
            key = (trade.instrument_id, trade.side.value, trade.entry_time)
            episodes.setdefault(key, []).append(trade)
        observations = []
        for index, trades in enumerate(episodes.values()):
            closing = max(t.exit_time for t in trades)
            observations.append(PerformanceObservation(
                f"{prefix}:{index}", trades[0].entry_time, closing, closing,
                sum(t.quantity * t.entry_price for t in trades), sum(t.gross_pnl for t in trades),
                sum(t.fees for t in trades), sum(t.net_pnl for t in trades)))
        return observations
