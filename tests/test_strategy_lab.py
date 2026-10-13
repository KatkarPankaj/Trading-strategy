import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from stockmarket.api.app import create_app
from stockmarket.core.ai import AIAnalyst
from stockmarket.core.ai.schemas import StrategyProposalSchema
from stockmarket.core.data.provider import DataUnavailable

import test_paper_end_to_end as paper_fixture
from test_paper_end_to_end import HEADERS
from test_personal_research import AS_OF, ResearchData


def specification():
    return {
        "outcome": "PROPOSAL", "template": "orb_vwap", "market": "US", "timeframe": "5m",
        "hypothesis": "Volume-confirmed opening-range breakout in a trending regime.",
        "invalidation": ["Missing volume confirmation"], "alternatives": ["NO_TRADE if data is stale"],
        "parameters": {"volume_ma_window": 20, "volume_spike_threshold": 1.2,
                       "stop_loss_pct": .004, "take_profit_pct": .008, "allow_short": False},
        "entry_rule": "OPENING_RANGE_BREAKOUT_WITH_VWAP_AND_VOLUME",
        "exit_rule": "STOP_TARGET_OR_SESSION_FLATTEN",
        "required_data": "VALID_COMPLETED_OHLCV_AND_VERIFIED_SESSION",
        "sizing_policy": "INDEPENDENT_APPLICATION_RISK_AND_SIZING",
        "rationale": "Research hypothesis, not risk authorization.",
    }


class LabAI:
    name = "fixture-laboratory"

    def __init__(self):
        self.output = specification()
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return json.dumps(self.output)


class StrategyLabTests(unittest.TestCase):
    def setUp(self):
        self.fixture = paper_fixture.PaperEndToEndTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ctx = self.fixture.context()
        self.client = self.fixture.client(self.ctx)
        run = self.fixture.run_research(self.client)
        self.snapshot_id = run["candidates"][0]["snapshot_id"]
        self.ai = LabAI()
        self.ctx.candidate_assessment.analyst = AIAnalyst(
            self.ai, clock=self.fixture.clock)

    def propose(self):
        return self.client.post("/strategy-lab/proposals", headers=HEADERS,
                                json={"snapshot_id": self.snapshot_id})

    def test_versioned_proposal_is_read_only_non_executable_and_retried_once(self):
        initial = self.fixture.state(self.client)
        proposal = self.propose()
        self.assertEqual(proposal.status_code, 200, proposal.text)
        body = proposal.json()
        self.assertFalse(body["executable"])
        self.assertEqual(body["validation_status"], "AWAITING_EVALUATION")
        self.assertEqual(self.propose().json()["proposal_id"], body["proposal_id"])
        before_calls = self.ai.calls
        for _ in range(2):
            monitoring = self.client.get("/strategy-lab/proposals", headers=HEADERS)
            self.assertEqual(len(monitoring.json()["proposals"]), 1)
            self.client.get("/paper/outcomes", headers=HEADERS)
            self.client.get("/paper/strategy-performance", headers=HEADERS)
        self.assertEqual(self.ai.calls, before_calls)
        self.assertEqual(self.fixture.state(self.client), initial)
        self.assertEqual(set(self.ctx.signal_generation.strategies), {"orb_vwap"})
        self.assertEqual(self.client.get("/strategy-lab/proposals").status_code, 401)

    def test_untrusted_code_unsupported_template_market_and_short_are_rejected(self):
        for field, value in (("python", "exec('trade')"), ("template", "untrusted"),
                             ("market", "IN")):
            self.ai.output = {**specification(), field: value}
            self.assertEqual(self.propose().status_code, 422)
        self.ai.output = specification()
        self.ai.output["parameters"]["allow_short"] = True
        self.assertEqual(self.propose().status_code, 422)
        self.assertEqual(self.ctx.store.strategy_lab.recent(), [])
        self.assertEqual(self.ctx.store.fills.all(), [])

    def test_no_trade_is_persisted_and_cannot_be_evaluated(self):
        self.ai.output["outcome"] = "NO_TRADE"
        proposal = self.propose().json()
        self.assertEqual(proposal["validation_status"], "NO_TRADE")
        evaluated = self.client.post(
            f"/strategy-lab/proposals/{proposal['proposal_id']}/evaluate", headers=HEADERS,
            json={"start": (AS_OF - timedelta(days=1)).isoformat(), "end": AS_OF.isoformat()})
        self.assertEqual(evaluated.status_code, 422)
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_small_history_reports_blocker_and_provider_failure_is_visible(self):
        proposal_id = self.propose().json()["proposal_id"]
        body = {"start": (AS_OF - timedelta(days=1)).isoformat(), "end": AS_OF.isoformat()}
        response = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(report["folds"], [])
        self.assertFalse(report["executable"])
        self.assertEqual(self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS,
            json=body).json()["evaluation_id"], report["evaluation_id"])
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv",
                          side_effect=DataUnavailable("fixture outage")):
            response = self.client.post(
                f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS, json=body)
        self.assertEqual(response.status_code, 503)

    def test_actual_cost_aware_walk_forward_uses_trusted_strategy_and_stays_non_executable(self):
        proposal_id = self.propose().json()["proposal_id"]
        self.fixture.clock.at = datetime(2026, 12, 10, 21, tzinfo=timezone.utc)
        market = self.ctx.markets.get("US")
        frames = []
        for day in pd.date_range("2026-10-01", "2026-12-09", freq="B"):
            if not market.calendar.is_trading_day(day.date()):
                continue
            index = pd.date_range(
                f"{day.date()} 09:30", f"{day.date()} {market.calendar.close_time_on(day.date())}",
                freq="5min", tz="America/New_York", inclusive="left")
            remaining = len(index) - 29
            frames.append(ResearchData.frame(
                index, [100.0] * 29 + [102.0, 103.0] + [104.0] * (remaining - 2),
                [1000.0] * 29 + [10000.0] * remaining))
        bars = pd.concat(frames)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            report = self.ctx.strategy_lab.evaluate(
                proposal_id, start=bars.index[0].to_pydatetime(),
                end=bars.index[-1].to_pydatetime() + timedelta(minutes=5))
        self.assertGreater(len(report["folds"]), 0)
        self.assertGreater(report["folds"][0]["test"]["metrics"]["trades"], 0)
        self.assertEqual(report["costs"]["latency_bars"], 1)
        self.assertGreater(report["costs"]["commission_rate"], 0)
        self.assertFalse(report["executable"])
        self.assertEqual(self.ctx.store.fills.all(), [])
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_parameter_boundaries_and_aware_dates(self):
        spec = specification()
        spec["parameters"]["stop_loss_pct"] = .99
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            StrategyProposalSchema.model_validate(spec)
        proposal_id = self.propose().json()["proposal_id"]
        response = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS,
            json={"start": "2026-10-01", "end": "2026-10-08"})
        self.assertEqual(response.status_code, 422)

    def holdout_request(self, proposal_id, start="2026-10-12T09:30:00-04:00",
                        end="2026-11-10T16:00:00-05:00"):
        body = {"start": start, "end": end}
        response = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/holdout", headers=HEADERS, json=body)
        return response, body

    def test_holdout_registration_is_future_immutable_and_requires_embargo(self):
        proposal_id = self.propose().json()["proposal_id"]
        before = self.fixture.state(self.client)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv") as provider:
            response, body = self.holdout_request(proposal_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.holdout_request(proposal_id)[0].json(), response.json())
            provider.assert_not_called()
        changed = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/holdout", headers=HEADERS,
            json={**body, "slippage_bps": 10})
        self.assertEqual(changed.status_code, 422)
        premature = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS,
            json={**body, "evaluation_kind": "POST_PROPOSAL_HOLDOUT"})
        self.assertEqual(premature.status_code, 422)
        self.assertEqual(self.fixture.state(self.client), before)
        self.ai.output["hypothesis"] = "A second frozen hypothesis."
        second = self.propose().json()["proposal_id"]
        no_embargo, _ = self.holdout_request(
            second, start="2026-10-09T09:30:00-04:00", end="2026-11-10T16:00:00-05:00")
        self.assertEqual(no_embargo.status_code, 422)
        past, _ = self.holdout_request(
            second, start="2026-10-05T09:30:00-04:00")
        self.assertEqual(past.status_code, 422)

    def test_holdout_requires_registration_and_excludes_exploratory_overlap(self):
        proposal_id = self.propose().json()["proposal_id"]
        evaluated = self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS, json={
                "start": "2026-10-01T09:30:00-04:00", "end": AS_OF.isoformat(),
                "evaluation_kind": "POST_PROPOSAL_HOLDOUT"})
        self.assertEqual(evaluated.status_code, 422)
        response, body = self.holdout_request(proposal_id)
        self.assertEqual(response.status_code, 200)
        self.fixture.clock.at = datetime(2026, 11, 11, 21, tzinfo=timezone.utc)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv") as provider:
            exploratory = self.client.post(
                f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS, json=body)
            self.assertEqual(exploratory.status_code, 422)
            provider.assert_not_called()

    def holdout_bars(self):
        market = self.ctx.markets.get("US")
        frames = []
        for day in pd.date_range("2026-10-12", "2026-11-10", freq="B"):
            if not market.calendar.is_trading_day(day.date()):
                continue
            index = pd.date_range(
                f"{day.date()} 09:30", f"{day.date()} {market.calendar.close_time_on(day.date())}",
                freq="5min", tz="America/New_York", inclusive="left")
            # Small breakouts followed by a flat price leave positions until flatten.
            frames.append(ResearchData.frame(
                index, [100.] * 29 + [100.5] * (len(index) - 29),
                [1000.] * 29 + [10000.] * (len(index) - 29)))
        return pd.concat(frames)

    def test_registered_holdout_runs_once_survives_restart_without_execution(self):
        proposal_id = self.propose().json()["proposal_id"]
        response, body = self.holdout_request(proposal_id)
        self.assertEqual(response.status_code, 200)
        self.fixture.clock.at = datetime(2026, 11, 11, 21, tzinfo=timezone.utc)
        bars = self.holdout_bars()
        initial = self.fixture.state(self.client)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            report = self.client.post(
                f"/strategy-lab/proposals/{proposal_id}/evaluate", headers=HEADERS,
                json={**body, "evaluation_kind": "POST_PROPOSAL_HOLDOUT"})
        self.assertEqual(report.status_code, 200, report.text)
        result = report.json()
        self.assertEqual(result["evaluation_kind"], "POST_PROPOSAL_HOLDOUT")
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertGreater(len(result["holdout_result"]["trades"]), 0)
        self.assertTrue(all(t["exit_reason"] == "END_OF_DAY_FLATTEN"
                            for t in result["holdout_result"]["trades"]))
        self.assertFalse(result["executable"])
        restarted = self.fixture.context()
        with patch.object(restarted.strategy_lab.market_data, "get_ohlcv") as provider:
            retry = restarted.strategy_lab.evaluate(
                proposal_id, start=datetime.fromisoformat(body["start"]),
                end=datetime.fromisoformat(body["end"]), evaluation_kind="POST_PROPOSAL_HOLDOUT")
            self.assertEqual(retry["evaluation_id"], result["evaluation_id"])
            provider.assert_not_called()
        self.assertEqual(self.fixture.state(self.client), initial)
        self.assertEqual(len(self.ctx.store.strategy_lab.evaluations(proposal_id)), 1)

    def test_holdout_rejects_missing_session_bars_and_policy_changes(self):
        proposal_id = self.propose().json()["proposal_id"]
        self.assertEqual(self.holdout_request(proposal_id)[0].status_code, 200)
        self.fixture.clock.at = datetime(2026, 11, 11, 21, tzinfo=timezone.utc)
        bars = self.holdout_bars()
        start = datetime.fromisoformat("2026-10-12T09:30:00-04:00")
        end = datetime.fromisoformat("2026-11-10T16:00:00-05:00")
        from stockmarket.core.strategy_lab import StrategyLabError
        missing_day = bars.loc[bars.index.date != datetime(2026, 10, 15).date()]
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=missing_day):
            with self.assertRaisesRegex(StrategyLabError, "missing complete trading sessions"):
                self.ctx.strategy_lab.evaluate(
                    proposal_id, start=start, end=end, evaluation_kind="POST_PROPOSAL_HOLDOUT")
        self.ctx.strategy_lab.flatten_minutes = 15
        with self.assertRaisesRegex(StrategyLabError, "frozen evaluation policy changed"):
            self.ctx.strategy_lab.evaluate(
                proposal_id, start=start, end=end, evaluation_kind="POST_PROPOSAL_HOLDOUT")

    def test_holdout_monitoring_remains_read_only_and_auth_required(self):
        proposal_id = self.propose().json()["proposal_id"]
        self.assertEqual(self.holdout_request(proposal_id)[0].status_code, 200)
        calls = self.ai.calls
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv") as provider:
            view = self.client.get("/strategy-lab/proposals", headers=HEADERS).json()
            self.assertIsNotNone(view["proposals"][0]["holdout"]["registered_at"])
            self.assertEqual(view["proposals"][0]["evaluations"], [])
            provider.assert_not_called()
        self.assertEqual(self.ai.calls, calls)
        self.assertEqual(self.client.post(
            f"/strategy-lab/proposals/{proposal_id}/holdout",
            json={"start": "2026-10-12T09:30:00-04:00", "end": "2026-11-10T16:00:00-05:00"}
        ).status_code, 401)

    def test_historical_truncated_session_is_not_used_as_flatten_price(self):
        proposal_id = self.propose().json()["proposal_id"]
        self.fixture.clock.at = datetime(2026, 12, 10, 21, tzinfo=timezone.utc)
        market = self.ctx.markets.get("US")
        frames = []
        for day in pd.date_range("2026-10-01", "2026-12-09", freq="B"):
            if not market.calendar.is_trading_day(day.date()):
                continue
            index = pd.date_range(f"{day.date()} 09:30", periods=35,
                                  freq="5min", tz="America/New_York")
            frames.append(ResearchData.frame(index, [100.] * len(index), [1000.] * len(index)))
        bars = pd.concat(frames)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            with self.assertRaisesRegex(ValueError, "missing scheduled session-flatten bar"):
                self.ctx.strategy_lab.evaluate(
                    proposal_id, start=bars.index[0].to_pydatetime(),
                    end=bars.index[-1].to_pydatetime() + timedelta(minutes=5))
        self.assertEqual(self.ctx.store.strategy_lab.evaluations(proposal_id), [])

    def test_holdout_sample_threshold_allows_review_not_execution(self):
        proposal_id = self.propose().json()["proposal_id"]
        self.assertEqual(self.holdout_request(proposal_id)[0].status_code, 200)
        self.fixture.clock.at = datetime(2026, 11, 11, 21, tzinfo=timezone.utc)
        bars = self.holdout_bars()
        for day in sorted(set(bars.index.date)):
            indices = bars.index[bars.index.date == day]
            for offset in (29, 50):
                bars.loc[indices[offset], ["open", "high", "low", "close", "volume"]] = [
                    100.5, 100.7, 100.3, 100.5, 10000]
                bars.loc[indices[offset + 1], ["open", "high", "low", "close", "volume"]] = [
                    100.5, 102.2, 100.3, 102, 10000]
            between = indices[31:50]
            bars.loc[between, ["open", "high", "low", "close", "volume"]] = [
                100, 100.2, 99.8, 100, 1000]
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=bars):
            report = self.ctx.strategy_lab.evaluate(
                proposal_id, start=datetime.fromisoformat("2026-10-12T09:30:00-04:00"),
                end=datetime.fromisoformat("2026-11-10T16:00:00-05:00"),
                evaluation_kind="POST_PROPOSAL_HOLDOUT")
        self.assertGreaterEqual(report["holdout_result"]["session_count"], 20)
        self.assertGreaterEqual(report["held_out_trade_count"], 30)
        self.assertEqual(report["status"], "HOLDOUT_REVIEW_REQUIRED")
        self.assertFalse(report["executable"])
        self.assertEqual(self.ctx.store.orders.all(), [])
        self.assertEqual(set(self.ctx.signal_generation.strategies), {"orb_vwap"})
