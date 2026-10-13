from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import patch
import unittest

import pandas as pd

from stockmarket.core.strategy_comparison import StrategyComparisonService
from stockmarket.core.strategy_lab import StrategyLabError
import test_strategy_lab as lab_fixture
from test_personal_research import ResearchData
from test_paper_end_to_end import HEADERS


class StrategyComparisonTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lab_fixture.StrategyLabTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ctx, self.client = self.fixture.ctx, self.fixture.client
        self.pid = self.fixture.propose().json()["proposal_id"]
        self.service = StrategyComparisonService(self.ctx.strategy_lab)
        self.fixture.fixture.clock.at = datetime(2026, 12, 20, 21, tzinfo=timezone.utc)
        market = self.ctx.markets.get("US")
        frames = []
        for stamp in pd.date_range("2026-10-01", "2026-12-18", freq="B"):
            if not market.calendar.is_trading_day(stamp.date()):
                continue
            index = pd.date_range(f"{stamp.date()} 09:30",
                f"{stamp.date()} {market.calendar.close_time_on(stamp.date())}",
                freq="5min", tz="America/New_York", inclusive="left")
            remaining = len(index) - 29
            frames.append(ResearchData.frame(
                index, [100.] * 29 + [102., 103.] + [104.] * (remaining - 2),
                [1000.] * 29 + [10000.] * remaining))
        self.bars = pd.concat(frames)
        self.start = self.bars.index[0].to_pydatetime()
        self.end = self.bars.index[-1].to_pydatetime() + timedelta(minutes=5)

    def compare(self, bars=None):
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv",
                          return_value=self.bars if bars is None else bars):
            return self.service.compare([self.pid], start=self.start, end=self.end)

    def test_actual_engine_persisted_comparison_abstains_on_small_sample_and_replays_without_data(self):
        initial = self.fixture.fixture.state(self.client)
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=self.bars):
            response = self.client.post("/strategy-lab/comparisons", headers=HEADERS, json={
                "proposal_ids": [self.pid], "start": self.start.isoformat(), "end": self.end.isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["improvement_status"], "NOT_ESTABLISHED")
        self.assertEqual(report["arms"]["learning_model_enhanced"]["status"], "UNAVAILABLE")
        self.assertEqual(report["arms"]["regime_conditioned_selection"]["status"], "UNAVAILABLE")
        self.assertGreater(report["arms"]["fixed_strategy"]["trade_count"], 0)
        self.assertGreater(report["arms"]["fixed_strategy"]["fees"], 0)
        self.assertEqual(report["arms"]["ranking_only"]["trade_count"], 0)
        self.assertEqual(report["arms"]["ranking_only"]["net_pnl"], 0)
        for fold in report["folds"]:
            self.assertEqual(fold["selected_strategy"], "NO_TRADE")
            self.assertLess(fold["window"]["validate"][1], fold["window"]["test"][0])
            self.assertTrue(all(s["score"] is None or s["score"] <= 0
                                for s in fold["selection_evidence"].values()))
        for previous, current in zip(report["folds"], report["folds"][1:]):
            self.assertLess(previous["window"]["test"][1], current["window"]["test"][0])
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", side_effect=AssertionError("read")):
            self.assertEqual(self.service.compare([self.pid], start=self.start, end=self.end), report)
            for _ in range(2):
                response = self.client.get(f"/strategy-lab/comparisons/{report['comparison_id']}", headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["monitoring_only"])
            response = self.client.get(f"/strategy-lab/comparisons/{report['comparison_id']}/data", headers=HEADERS)
            snapshot = response.json()
            restored = pd.DataFrame(snapshot["bars"], columns=snapshot["columns"],
                index=pd.DatetimeIndex(pd.to_datetime(snapshot["timestamps"], utc=True))
                    .tz_convert(snapshot["timezone"]).rename(snapshot["index_name"]))
            self.assertEqual(hashlib.sha256(restored.to_csv().encode()).hexdigest(), report["data_hash"])
        self.assertEqual(self.fixture.fixture.state(self.client), initial)
        self.assertEqual(self.ctx.store.orders.all(), [])
        self.assertEqual(self.ctx.store.strategy_lab.state(self.pid)["state"], "DRAFT")
        self.assertEqual(self.client.get(f"/strategy-lab/comparisons/{report['comparison_id']}").status_code, 401)
        restarted = self.fixture.fixture.context()
        with patch.object(restarted.strategy_lab.market_data, "get_ohlcv", side_effect=AssertionError("restart read")):
            self.assertEqual(StrategyComparisonService(restarted.strategy_lab).compare(
                [self.pid], start=self.start, end=self.end), report)
        print("STRATEGY_INTELLIGENCE deterministic comparison:", {
            "windows": len(report["folds"]),
            "fixed_trades": report["arms"]["fixed_strategy"]["trade_count"],
            "fixed_net_pnl": report["arms"]["fixed_strategy"]["net_pnl"],
            "fixed_fees": report["arms"]["fixed_strategy"]["fees"],
            "ranking_trades": report["arms"]["ranking_only"]["trade_count"],
            "paired": report["paired_comparison"], "improvement": report["improvement_status"]})

    def test_actual_positive_validation_can_select_a_version_without_using_test_results(self):
        frames = []
        for day in sorted(set(self.bars.index.date)):
            index = self.bars.loc[self.bars.index.date == day].index
            closes, volumes = [100.] * 29, [1_000_000.] * 29
            for n in range(5):
                price = 102. + n * 2
                closes += [price, price + .05, price + 1.5] + [price + 1.5] * 9
                volumes += [10_000_000.] + [1_000_000.] * 11
            frames.append(ResearchData.frame(index, closes[:len(index)], volumes[:len(index)]))
        report = self.compare(pd.concat(frames))
        self.assertTrue(any(f["selected_strategy"] != "NO_TRADE" for f in report["folds"]))
        for fold in report["folds"]:
            selected = fold["selected_strategy"]
            if selected != "NO_TRADE":
                sample = fold["selection_evidence"][selected]
                self.assertGreater(sample["score"], 0)
                self.assertGreaterEqual(sample["trade_count"], 30)
                self.assertGreaterEqual(sample["observed_days"], 10)
                self.assertGreater(fold["ranking_metrics"]["trades"], 0)
        self.assertGreater(report["arms"]["ranking_only"]["fees"], 0)
        self.assertEqual(report["improvement_status"], "NOT_ESTABLISHED")
        self.assertEqual(self.ctx.store.orders.all(), [])

    def test_future_test_prices_do_not_change_prior_validation_or_selection(self):
        original = self.compare()
        first_test = original["folds"][0]["window"]["test"][0]
        changed = self.bars.copy()
        future = changed.index.date >= datetime.fromisoformat(first_test).date()
        changed.loc[future, ["open", "high", "low", "close"]] *= 1.5
        second = lab_fixture.StrategyLabTests()
        second.setUp()
        self.addCleanup(second.doCleanups)
        second.fixture.clock.at = self.fixture.fixture.clock.at
        pid = second.propose().json()["proposal_id"]
        with patch.object(second.ctx.strategy_lab.market_data, "get_ohlcv", return_value=changed):
            altered = StrategyComparisonService(second.ctx.strategy_lab).compare(
                [pid], start=self.start, end=self.end)
        self.assertNotEqual(original["data_hash"], altered["data_hash"])
        a, b = original["folds"][0], altered["folds"][0]
        self.assertEqual(a["selected_strategy"], b["selected_strategy"])
        self.assertEqual(a["selection_evidence"]["fixed-orb"], b["selection_evidence"]["fixed-orb"])
        self.assertEqual(a["selection_evidence"][self.pid], b["selection_evidence"][pid])
        self.assertNotEqual(a["fixed_metrics"]["turnover"], b["fixed_metrics"]["turnover"])

    def test_complete_missing_session_partial_range_and_reserved_holdout_fail_closed(self):
        missing_day = self.bars.index.tz_convert("America/New_York").date[78]
        missing = self.bars.loc[self.bars.index.date != missing_day]
        with self.assertRaisesRegex(StrategyLabError, "missing historical"):
            self.compare(missing)
        with self.assertRaisesRegex(StrategyLabError, "complete exchange sessions"):
            with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", return_value=self.bars.iloc[1:]):
                self.service.compare([self.pid], start=self.start + timedelta(minutes=5), end=self.end)
        self.ctx.store.strategy_lab.save_holdout({
            "proposal_id": self.pid, "start": self.start.isoformat(), "end": self.end.isoformat()},
            self.start - timedelta(days=2))
        with patch.object(self.ctx.strategy_lab.market_data, "get_ohlcv", side_effect=AssertionError("leak")):
            with self.assertRaisesRegex(StrategyLabError, "reserved final holdout"):
                self.service.compare([self.pid], start=self.start, end=self.end)

    def test_comparison_mutation_requires_authentication_and_valid_boundary_input(self):
        body = {"proposal_ids": [self.pid], "start": self.start.isoformat(), "end": self.end.isoformat()}
        self.assertEqual(self.client.post("/strategy-lab/comparisons", json=body).status_code, 401)
        self.assertEqual(self.client.post("/strategy-lab/comparisons", headers=HEADERS,
            json={**body, "proposal_ids": [self.pid, self.pid]}).status_code, 422)
        self.assertEqual(self.client.post("/strategy-lab/comparisons", headers=HEADERS,
            json={**body, "start": self.start.replace(tzinfo=None).isoformat()}).status_code, 422)
