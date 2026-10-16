from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import pandas as pd

import test_strategy_lab as lab_fixture
from test_paper_end_to_end import HEADERS
from test_personal_research import AS_OF, ResearchData, assessment_response

from stockmarket.core.ai import AIAnalyst
from stockmarket.core.autonomous_research import AutonomousResearchService
from stockmarket.core.strategy_lab import StrategyLabError


class SelectionAI:
    name = "fixture-version-selection"

    def __init__(self, name):
        self.strategy = name

    def complete(self, system, user):
        payload = json.loads(user.split("<untrusted>\n", 1)[1].split("\n</untrusted>", 1)[0])
        if "available_strategies" in payload:
            return json.dumps({
                "summary": "Select the independently reviewed paper version.",
                "ranked_strategies": [{"strategy": self.strategy, "confidence": .8,
                                       "rationale": "Reviewed version fits the current evidence."}],
                "risks": [], "data_gaps": []})
        return json.dumps(assessment_response(
            instrument_id=payload["instrument_id"], snapshot_id=payload["snapshot_id"],
            key_evidence=[e["evidence_id"] for e in payload["evidence"] if e["component"] == "technical"]))


class StrategyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lab = lab_fixture.StrategyLabTests()
        self.lab.setUp()
        self.addCleanup(self.lab.doCleanups)
        self.ctx, self.client = self.lab.ctx, self.lab.client
        self.fixture = self.lab.fixture

    def transition(self, proposal_id, target, key, *, acknowledge=True):
        return self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/transition", headers=HEADERS,
            json={"target": target, "operator": "fixture-reviewer", "reason": "Reviewed costs and evidence",
                  "request_id": key, "acknowledge_selection_bias": acknowledge})

    def profitable_bars(self, start, end):
        frames = []
        calendar = self.ctx.markets.get("US").calendar
        for day in pd.date_range(start, end, freq="D"):
            if not calendar.is_trading_day(day.date()):
                continue
            index = pd.date_range(f"{day.date()} 09:30", f"{day.date()} {calendar.close_time_on(day.date())}",
                                  freq="5min", tz="America/New_York", inclusive="left")
            frame = ResearchData.frame(index, [100.] * len(index), [1000.] * len(index))
            for offset in (29, 50):
                if offset + 1 >= len(index):
                    continue
                frame.loc[index[offset], ["open", "high", "low", "close", "volume"]] = [
                    100.5, 100.7, 100.3, 100.5, 10000]
                frame.loc[index[offset + 1], ["open", "high", "low", "close", "volume"]] = [
                    100.5, 102.2, 100.3, 102, 10000]
            frames.append(frame)
        return pd.concat(frames)

    def evaluate_and_activate(self, template):
        self.lab.ai.output = lab_fixture.specification()
        if template == "sma_trend":
            self.lab.ai.output.update(template=template, entry_rule="SMA_CROSSOVER_WITH_PRIOR_VOLUME")
            self.lab.ai.output["parameters"].update(fast_window=2, slow_window=5,
                                                    volume_ma_window=5)
        proposed = self.lab.propose()
        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposal = proposed.json()
        pid = proposal["proposal_id"]
        bars = self.profitable_bars("2026-08-03", "2026-09-30")
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            self.ctx.strategy_lab.evaluate(pid, start=bars.index[0].to_pydatetime(),
                                          end=bars.index[-1].to_pydatetime() + timedelta(minutes=5))
        self.assertEqual(self.ctx.store.strategy_lab.state(pid)["state"], "EVALUATED")
        self.assertEqual(self.transition(pid, "APPROVED_FOR_PAPER", "too-early").status_code, 422)
        self.assertFalse(self.ctx.gate.halted)
        registered, _ = self.lab.holdout_request(pid)
        self.assertEqual(registered.status_code, 200, registered.text)
        self.fixture.clock.at = datetime(2026, 11, 11, 16, tzinfo=timezone.utc)
        bars = self.profitable_bars("2026-10-12", "2026-11-10")
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            report = self.ctx.strategy_lab.evaluate(
                pid, start=bars.index[0].to_pydatetime(),
                end=datetime.fromisoformat("2026-11-10T16:00:00-05:00"),
                evaluation_kind="POST_PROPOSAL_HOLDOUT")
        self.assertEqual(report["status"], "HOLDOUT_REVIEW_REQUIRED")
        self.assertTrue(self.ctx.strategy_lab.eligibility(pid)["eligible_for_review"],
                        self.ctx.strategy_lab.eligibility(pid))
        self.assertEqual(self.transition(pid, "ACTIVE_FOR_PAPER", "skip-approval").status_code, 422)
        self.assertEqual(self.transition(pid, "APPROVED_FOR_PAPER", "missing-ack", acknowledge=False).status_code, 422)
        approved = self.transition(pid, "APPROVED_FOR_PAPER", "approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(self.transition(pid, "APPROVED_FOR_PAPER", "approve").json()["duplicate"])
        active = self.transition(pid, "ACTIVE_FOR_PAPER", "activate")
        self.assertEqual(active.status_code, 200, active.text)
        name = "lab_" + pid
        self.assertIn(name, self.ctx.signal_generation.strategies)
        self.assertIn(name, self.ctx.autonomous_research.settings.strategy_allowlist)
        self.assertEqual(self.ctx.store.orders.all(), [])
        return proposal, name

    def _prepare_automatic(self):
        proposal, name = self.evaluate_and_activate("orb_vwap")
        clock = self.fixture.clock
        for attr in ("intraday", "daily"):
            setattr(self.fixture.data, attr, getattr(ResearchData(), attr).rename(
                index=lambda at: at + (clock.at.date() - AS_OF.date())))
        clock.at = clock.at.replace(hour=16, minute=0)
        self.ctx.candidate_assessment.analyst = AIAnalyst(SelectionAI(name), clock=clock)
        # The shared fixture forces exploratory selection; restore the real validated path.
        vars(self.ctx.autonomous_research).pop("run", None)
        return name, clock

    def test_qualified_active_version_trades_automatically_through_all_gates(self):
        name, clock = self._prepare_automatic()
        self.ctx.candidate_assessment.automatic_evidence_max_age = timedelta(days=30)
        cycle = self.ctx.autonomous_paper_trading.run(
            "US_ALL", idempotency_key="qualified-automatic", operator="fixture",
            as_of=clock.at, top_n=1)
        candidate = cycle["candidates"][0]
        self.assertEqual(candidate["status"], "COMPLETE", candidate["payload"])
        trace = candidate["payload"]["decision_trace"]
        self.assertEqual((trace["final_status"], trace["strategy"]), ("FILLED", name))
        orders = self.ctx.store.orders.all()
        self.assertEqual(len(orders), 1)
        self.assertEqual((orders[0]["strategy"], orders[0]["status"], orders[0]["trade_intent"]),
                         (name, "FILLED", "BUY"))
        self.assertEqual(len(self.ctx.store.fills.all()), 1)
        replay = self.ctx.autonomous_paper_trading.run(
            "US_ALL", idempotency_key="qualified-automatic", operator="fixture",
            as_of=clock.at, top_n=1)
        self.assertEqual(replay["run_id"], cycle["run_id"])
        self.assertEqual(len(self.ctx.store.orders.all()), 1)

    def test_ai_cannot_choose_an_unqualified_strategy(self):
        name, clock = self._prepare_automatic()
        self.ctx.candidate_assessment.automatic_evidence_max_age = timedelta(days=30)
        self.ctx.candidate_assessment.analyst = AIAnalyst(SelectionAI("orb_vwap"), clock=clock)
        cycle = self.ctx.autonomous_paper_trading.run(
            "US_ALL", idempotency_key="unqualified-choice", operator="fixture",
            as_of=clock.at, top_n=1)
        candidate = cycle["candidates"][0]
        self.assertEqual(candidate["status"], "RESEARCH_REJECTED", candidate["payload"])
        self.assertNotEqual(candidate["payload"]["research_assessment"].get("recommended_strategy"),
                            "orb_vwap")
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_expired_or_unconfigured_evidence_keeps_automatic_entries_blocked(self):
        name, clock = self._prepare_automatic()
        for key, max_age, reason in (("unset", None, "AUTOMATIC_EVIDENCE_EXPIRY_UNSET"),
                                     ("expired", timedelta(hours=1), "AUTOMATIC_EVIDENCE_EXPIRED")):
            with self.subTest(key=key):
                self.ctx.candidate_assessment.automatic_evidence_max_age = max_age
                cycle = self.ctx.autonomous_paper_trading.run(
                    "US_ALL", idempotency_key=f"blocked-{key}", operator="fixture",
                    as_of=clock.at, top_n=1)
                assessment = cycle["candidates"][0]["payload"]["research_assessment"]
                self.assertEqual(assessment["status"], "NO_VALIDATED_STRATEGY")
                self.assertIn(f"{name}={reason}", assessment["error"])
                self.assertIn("orb_vwap=NOT_A_QUALIFIED_LAB_VERSION", assessment["error"])
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_auth_illegal_transitions_no_trade_and_request_identity(self):
        pid = self.lab.propose().json()["proposal_id"]
        response = self.client.post(f"/strategy-lab/proposals/{pid}/transition", json={
            "target": "ACTIVE_FOR_PAPER", "operator": "fixture", "reason": "review", "request_id": "unauth"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transition(pid, "ACTIVE_FOR_PAPER", "invalid").status_code, 422)
        self.assertEqual(self.ctx.store.strategy_lab.state(pid)["state"], "DRAFT")
        self.assertFalse(self.ctx.gate.halted)
        retired = self.transition(pid, "RETIRED", "retire")
        self.assertEqual(retired.status_code, 200, retired.text)
        self.assertTrue(self.transition(pid, "RETIRED", "retire").json()["duplicate"])
        self.assertEqual(self.transition(pid, "PAUSED", "retire").status_code, 422)
        self.assertEqual(self.transition(pid, "ACTIVE_FOR_PAPER", "resume").status_code, 422)
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_complete_proposal_activation_execution_outcome_restart_both_templates(self):
        # Separate fixture state per template; no synthetic report or bypassed gate.
        for template in ("orb_vwap", "sma_trend"):
            with self.subTest(template=template):
                if template == "sma_trend":
                    self.lab.doCleanups()
                    self.setUp()
                proposal, name = self.evaluate_and_activate(template)
                clock = self.fixture.clock
                self.fixture.data.intraday = ResearchData().intraday.rename(
                    index=lambda at: at + (clock.at.date() - AS_OF.date()))
                self.fixture.data.daily = ResearchData().daily.rename(
                    index=lambda at: at + (clock.at.date() - AS_OF.date()))
                clock.at = clock.at.replace(hour=16, minute=0)
                self.ctx.candidate_assessment.analyst = AIAnalyst(SelectionAI(name), clock=clock)
                run = self.client.post("/research/autonomous", headers=HEADERS, json={
                    "universe_id": "US_ALL", "idempotency_key": "active-version-research",
                    "as_of": clock.at.isoformat(), "top_n": 1})
                self.assertEqual(run.status_code, 200, run.text)
                automatic = AutonomousResearchService.run(
                    self.ctx.autonomous_research,
                    "US_ALL", idempotency_key="reviewed-is-not-automatic",
                    as_of=clock.at, top_n=1, validated_selection=True)
                blocked = automatic["candidates"][0]["opportunity"]["assessment"]
                self.assertEqual(blocked["status"], "NO_VALIDATED_STRATEGY")
                self.assertIn(f"{name}=AUTOMATIC_EVIDENCE_EXPIRY_UNSET", blocked["error"])
                self.assertIsNone(blocked["recommended_strategy"])
                candidate = run.json()["candidates"][0]
                base = f"/research/autonomous/{run.json()['run_id']}/candidates/{candidate['instrument_id']}"
                signal = self.client.post(base + "/signal", headers=HEADERS,
                                          json={"evaluation_as_of": clock.at.isoformat()})
                self.assertEqual(signal.status_code, 200, signal.text)
                self.assertEqual(signal.json()["status"], "SIGNAL_GENERATED", signal.json())
                self.assertEqual(signal.json()["signal"]["strategy"], name)
                risk = self.client.post(base + "/risk", headers=HEADERS, json={})
                self.assertEqual(risk.status_code, 200, risk.text)
                self.assertEqual(risk.json()["status"], "RISK_APPROVED", risk.json())
                from dataclasses import replace
                from stockmarket.core.trade_proposals import TradeProposalService
                trusted = TradeProposalService._signal_from_row(
                    self.ctx.store.signals.get(risk.json()["signal_id"]))
                forged = self.ctx.paper.submit_signal(
                    replace(trusted, reasons=("UNTRUSTED_REPLACEMENT",)), quantity=100,
                    client_order_id="forged-version-signal")
                self.assertEqual(forged.order.status.value, "REJECTED")
                self.assertEqual(forged.risk.reason, "PAPER_STRATEGY_SIGNAL_OR_PROTECTION_CHANGED")
                self.assertEqual(self.ctx.store.fills.all(), [])
                self.assertEqual(TradeProposalService._signal_from_row(
                    self.ctx.store.signals.get(risk.json()["signal_id"])), trusted)
                entry = self.fixture.submit(self.client, risk.json()["proposal_id"])
                self.assertEqual(entry.status_code, 200, entry.text)
                self.assertEqual(entry.json()["order"]["status"], "FILLED", entry.json())
                quantity = self.ctx.paper.portfolio.positions()[self.fixture.instrument_id].quantity
                self.assertEqual(self.ctx.paper.portfolio.cash["USD"], 100000 - quantity * 102)
                self.assertEqual(self.ctx.store.orders.all()[-1]["trade_intent"], "BUY")
                paused = self.transition(proposal["proposal_id"], "PAUSED", "pause")
                self.assertEqual(paused.status_code, 200, paused.text)
                self.assertNotIn(name, self.ctx.signal_generation.strategies)
                rejected = self.ctx.paper.submit_signal(
                    trusted, quantity=100, client_order_id="paused-version-entry")
                self.assertEqual(rejected.risk.reason, "PAPER_STRATEGY_NOT_ACTIVE")
                clock.at += timedelta(minutes=1)
                self.fixture.data.price = 100
                self.ctx.paper_position_manager.manage()
                self.assertEqual(self.ctx.paper.portfolio.positions(), {})
                self.assertEqual(self.ctx.store.orders.all()[-1]["trade_intent"], "SELL_TO_CLOSE")
                self.assertEqual(self.ctx.paper.portfolio.cash["USD"], 100000 - 2 * quantity)
                outcomes = self.ctx.store.paper_outcomes.recent()
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["net_pnl"], -2 * quantity)
                self.assertEqual(outcomes[0]["attribution"]["strategy_version"], proposal["strategy_version_id"])
                resumed = self.transition(proposal["proposal_id"], "ACTIVE_FOR_PAPER", "resume")
                self.assertEqual(resumed.status_code, 200, resumed.text)
                restarted = self.fixture.context()
                self.assertIn(name, restarted.signal_generation.strategies)
                self.assertEqual(restarted.outcome_feedback.process(), 0)
                self.assertEqual(len(restarted.store.fills.all()), 2)
                feedback = restarted.strategy_ranking.evidence(
                    clock.at, "US", "USD", restarted.candidate_assessment.strategies)
                self.assertEqual(feedback["performance"][0]["trade_count"], 1)
                self.assertEqual(feedback["performance"][0]["sample_status"], "INSUFFICIENT_EVIDENCE")
                self.assertTrue(feedback["evaluations"])
                measurements = restarted.strategy_ranking.measurements()
                self.assertEqual(measurements["improvement_status"], "NOT_ESTABLISHED")
                measured = [r for r in measurements["rankings"] if r["ranked_order"] == [name]][0]
                self.assertEqual(measured["completed_forward_trades"], 1)
                self.assertEqual(measured["forward_net_pnl"], -2 * quantity)
                print(f"MILESTONE5 {template}: version={proposal['strategy_version_id']} quantity={quantity} "
                      f"cash=100000->{100000-quantity*102}->{100000-2*quantity}; "
                      f"fills=2 outcomes=1 net_pnl={-2*quantity}; restart=ACTIVE_FOR_PAPER")
