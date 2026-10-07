from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

from stockmarket.core.markets import default_markets
from stockmarket.core.models import (
    AssetClass,
    RiskDecisionStatus,
    Signal,
    SignalSide,
)
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.persistence.repositories import to_json
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.risk import RiskEngine, RiskLimits
from stockmarket.core.risk_portfolio import PortfolioRiskLimits
from stockmarket.core.sizing import SizingLimits
from stockmarket.core.trade_proposals import TradeProposalService


EVALUATED_AT = datetime(2026, 10, 5, 13, 50, tzinfo=timezone.utc)
DATA_AT = EVALUATED_AT - timedelta(minutes=2)
RUN_ID = "autonomous-run"
RESEARCH_RUN_ID = "research-run"
SCAN_ID = "scan"
SNAPSHOT_ID = "snapshot"
OPPORTUNITY_ID = "opportunity"
GENERATION_ID = "generation"
SIGNAL_ID = uuid4()
INSTRUMENT = replace(
    default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01),
    sector="TECH",
    shortable=True,
)


class _EmptyOrderManager:
    @staticmethod
    def orders():
        return ()

    @staticmethod
    def open_orders():
        return ()


class TradeProposalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = SQLiteDatabase()
        self.addCleanup(self.db.close)
        migrate(self.db)
        self.store = Store(self.db)
        self.seed_signal()
        self.portfolio = PortfolioManager("USD", 1_000_000.0)
        self.engine = self.make_engine()
        self.service = TradeProposalService(
            autonomous_repository=self.store.autonomous_research,
            research_repository=self.store.research_runs,
            opportunity_repository=self.store.research_runs,
            signal_generation_repository=self.store.signal_generations,
            signal_repository=self.store.signals,
            proposal_repository=self.store.trade_proposals,
            portfolio=self.portfolio,
            risk_engine=self.engine,
            sizing_limits=SizingLimits(
                risk_per_trade_pct=0.01,
                max_order_notional=50_000.0,
                cash_requirement_rate=1.0,
            ),
            instruments={INSTRUMENT.instrument_id: INSTRUMENT},
            markets=default_markets(),
            order_manager=_EmptyOrderManager(),
            market_stats=lambda _: {
                "correlated_exposure": 0.0,
                "average_daily_traded_value": 100_000_000.0,
                "expected_slippage_bps": 5.0,
            },
            clock=lambda: EVALUATED_AT,
        )

    def make_engine(self, *, minimum_expected_edge: float = 0.0) -> RiskEngine:
        return RiskEngine(
            RiskLimits(
                max_position_quantity=1_000,
                max_order_notional=50_000.0,
                max_open_positions=5,
                max_trades_per_day=10,
                cash_requirement_rate=1.0,
                entry_window=(time(9, 30), time(15, 55)),
                minimum_reward_risk=1.1,
                minimum_expected_edge=minimum_expected_edge,
                max_market_data_age=timedelta(minutes=5),
            ),
            PortfolioRiskLimits(
                max_risk_per_trade_pct=0.01,
                max_daily_loss_pct=0.02,
                max_drawdown_pct=0.10,
                max_position_notional_pct=0.20,
                max_total_notional_pct=1.0,
                max_sector_exposure_pct=0.30,
                max_correlated_exposure_pct=0.40,
                max_leverage=2.0,
                max_orders_per_minute=5,
                min_liquidity=1_000_000.0,
                max_slippage_bps=20.0,
                duplicate_order_prevention=True,
            ),
        )

    def seed_signal(
        self,
        *,
        side: SignalSide = SignalSide.BUY,
        signal_time: datetime = DATA_AT,
        stop: float | None = None,
        target: float | None = None,
    ) -> None:
        stop = stop if stop is not None else (
            99.0 if side is SignalSide.BUY else 101.0)
        target = target if target is not None else (
            102.0 if side is SignalSide.BUY else 98.0)
        signal = Signal(
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            timestamp=signal_time,
            strategy="orb_vwap",
            side=side,
            entry_price=100.0,
            stop_loss=stop,
            take_profit=target,
            reward_risk=2.0,
            confidence=0.9,
            expected_edge=100.0,
            signal_id=SIGNAL_ID,
        )
        now = EVALUATED_AT.isoformat()
        data_at = signal_time.isoformat()
        created_at = (EVALUATED_AT - timedelta(minutes=1)).isoformat()
        snapshot_payload = {
            "snapshot_id": SNAPSHOT_ID,
            "run_id": RESEARCH_RUN_ID,
            "instrument_id": INSTRUMENT.instrument_id,
            "market": INSTRUMENT.market,
            "as_of": (EVALUATED_AT - timedelta(minutes=2)).isoformat(),
            "created_at": created_at,
        }
        assessment_context = {
            "snapshot_id": SNAPSHOT_ID,
            "snapshot_created_at": created_at,
            "instrument_id": INSTRUMENT.instrument_id,
            "symbol": INSTRUMENT.symbol,
            "market": INSTRUMENT.market,
            "asset_class": INSTRUMENT.asset_class.value,
            "currency": INSTRUMENT.currency,
            "timezone": INSTRUMENT.timezone,
            "as_of": (EVALUATED_AT - timedelta(minutes=2)).isoformat(),
            "registered_strategies": [{
                "name": "orb_vwap",
                "implementation": "OrbVwapStrategy",
                "version": "1.0.0",
            }],
        }
        opportunity_payload = {
            "opportunity_id": OPPORTUNITY_ID,
            "state": "STRATEGY_SELECTED",
            "created_at": created_at,
            "assessment": {
                "snapshot_id": SNAPSHOT_ID,
                "instrument_id": INSTRUMENT.instrument_id,
                "status": "COMPLETE",
                "assessed_at": created_at,
                "recommended_strategy": "orb_vwap",
                "input_context": assessment_context,
            },
        }
        candidate_payload = {
            "instrument_id": INSTRUMENT.instrument_id,
            "snapshot_id": SNAPSHOT_ID,
            "stage": "STRATEGY_SELECTED",
            "opportunity": {
                "opportunity_id": OPPORTUNITY_ID,
                "state": "STRATEGY_SELECTED",
            },
        }
        generation_payload = {
            "provenance": {
                "run_id": RUN_ID,
                "instrument_id": INSTRUMENT.instrument_id,
                "strategy": "orb_vwap",
                "strategy_version": "1.0.0",
                "data_quality": "VALID",
                "evaluation_as_of": now,
            },
        }
        self.db.execute(
            """INSERT INTO scanner_runs
               (scan_id, universe_id, markets, mode, started_at, completed_at,
                status, requested_count, evaluated_count, accepted_count,
                rejected_count, failed_count, failure_summary, payload, as_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (SCAN_ID, "universe", "[]", "RESEARCH", now, now, "COMPLETE",
             1, 1, 1, 0, 0, "[]", "{}", now),
        )
        self.db.execute(
            """INSERT INTO research_runs
               (run_id, scan_id, as_of, created_at, status, requested_count,
                completed_count, failed_count, failure_summary, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (RESEARCH_RUN_ID, SCAN_ID, now, now, "COMPLETE", 1, 1, 0, "[]", "{}"),
        )
        self.db.execute(
            """INSERT INTO research_snapshots
               (snapshot_id, run_id, instrument_id, scanner_rank,
                scanner_score, as_of, status, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (SNAPSHOT_ID, RESEARCH_RUN_ID, INSTRUMENT.instrument_id, 1,
             50.0, now, "COMPLETE", to_json(snapshot_payload)),
        )
        self.db.execute(
            """INSERT INTO ai_research_assessments
               (assessment_id, snapshot_id, status, provider, model_version,
                prompt_version, schema_version, assessed_at, strategy_valid,
                recommended_strategy, error, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("assessment", SNAPSHOT_ID, "COMPLETE", "test", "1", "1", "1",
             now, 1, "orb_vwap", None, "{}"),
        )
        self.db.execute(
            """INSERT INTO research_opportunities
               (opportunity_id, assessment_id, state, ranking_score,
                lifecycle, created_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (OPPORTUNITY_ID, "assessment", "STRATEGY_SELECTED", 50.0,
             to_json({"state": "STRATEGY_SELECTED"}), created_at,
             to_json(opportunity_payload)),
        )
        request = {
            "mode": "PAPER",
            "as_of": (EVALUATED_AT - timedelta(minutes=2)).isoformat(),
        }
        self.db.execute(
            """INSERT INTO autonomous_research_runs
               (run_id, idempotency_key, request_hash, status, stage,
                created_at, updated_at, as_of, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (RUN_ID, "run-key", "hash", "COMPLETE", "STRATEGY_SELECTED",
             now, now, request["as_of"], to_json({"request": request})),
        )
        self.db.execute(
            """INSERT INTO autonomous_research_candidates
               (run_id, instrument_id, stage, snapshot_id, opportunity_id,
                error, updated_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (RUN_ID, INSTRUMENT.instrument_id, "SIGNAL_GENERATED",
             SNAPSHOT_ID, OPPORTUNITY_ID, None, now, to_json(candidate_payload)),
        )
        self.db.execute(
            """INSERT INTO signals
               (signal_id, instrument_id, symbol, timestamp, strategy, side,
                confidence, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(signal.signal_id), signal.instrument_id, signal.symbol,
             signal.timestamp.isoformat(), signal.strategy, signal.side.value,
             signal.confidence, to_json({
                 "entry_price": signal.entry_price,
                 "stop_loss": signal.stop_loss,
                 "take_profit": signal.take_profit,
                 "reward_risk": signal.reward_risk,
                 "expected_edge": signal.expected_edge,
                 "regime": signal.regime,
                 "reasons": signal.reasons,
                 "invalidation_conditions": signal.invalidation_conditions,
             })),
        )
        self.db.execute(
            """INSERT INTO generated_strategy_signals
               (generation_id, idempotency_key, run_id, candidate_id,
                opportunity_id, snapshot_id, instrument_id, strategy_name,
                strategy_version, evaluation_as_of, data_timestamp,
                input_fingerprint, status, reason, signal_id, generated_at,
                payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (GENERATION_ID, "generation-key", RUN_ID, INSTRUMENT.instrument_id,
             OPPORTUNITY_ID, SNAPSHOT_ID, INSTRUMENT.instrument_id,
             "orb_vwap", "1.0.0", now, data_at, "fingerprint",
             "SIGNAL_GENERATED", None, str(signal.signal_id), now,
             to_json(generation_payload)),
        )

    def test_buy_signal_is_sized_risk_gated_persisted_and_never_submitted(self) -> None:
        original_cash = self.portfolio.cash_base
        original_positions = self.portfolio.positions()

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_APPROVED", result["rejection_reason"])
        proposal = result["trade_proposal"]
        self.assertGreater(proposal["quantity"], 0)
        self.assertEqual(proposal["side"], "BUY")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        self.assertIsNone(result["order_id"])
        self.assertEqual(result["risk_decision"].status, RiskDecisionStatus.APPROVED)
        self.assertEqual(self.portfolio.cash_base, original_cash)
        self.assertEqual(self.portfolio.positions(), original_positions)
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])
        self.assertEqual(self.db.query("SELECT * FROM fills"), [])
        self.assertEqual(self.db.query("SELECT * FROM positions"), [])
        self.assertEqual(len(self.store.trade_proposals.approved()), 1)
        stored = self.store.trade_proposals.get_by_id(proposal["proposal_id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["evaluation_status"], "APPROVED")
        self.assertEqual(stored["payload"]["signal_id"], proposal["signal_id"])

        repeated = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len(self.db.query("SELECT * FROM risk_evaluations")), 1)
        self.assertEqual(len(self.db.query("SELECT * FROM trade_proposals")), 1)

    def test_short_entry_uses_persisted_sell_signal_and_is_terminal(self) -> None:
        self.db.execute(
            "UPDATE signals SET side = ?, payload = ? WHERE signal_id = ?",
            ("SELL", to_json({
                "entry_price": 100.0,
                "stop_loss": 101.0,
                "take_profit": 98.0,
                "reward_risk": 2.0,
                "expected_edge": 100.0,
                "regime": "TRENDING_DOWN",
                "reasons": [],
                "invalidation_conditions": [],
            }), str(SIGNAL_ID)),
        )

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_APPROVED", result["rejection_reason"])
        self.assertEqual(result["trade_proposal"]["side"], "SELL")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])
        self.assertEqual(self.portfolio.positions(), {})

    def test_risk_rejection_is_persisted_without_a_proposal(self) -> None:
        self.service.risk_engine = self.make_engine(minimum_expected_edge=101.0)

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertEqual(result["risk_decision"].status, RiskDecisionStatus.REJECTED)
        self.assertIsNotNone(result["rejection_reason"])
        self.assertIsNone(result["trade_proposal"])
        self.assertGreater(result["risk_amount"], 0)
        self.assertGreater(result["risk_percentage"], 0)
        self.assertEqual(len(self.db.query("SELECT * FROM risk_evaluations")), 1)
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])
        self.assertEqual(self.portfolio.positions(), {})

    def test_stale_signal_is_rejected_and_hard_stop_is_required(self) -> None:
        self.db.execute(
            "UPDATE generated_strategy_signals SET data_timestamp = ? "
            "WHERE generation_id = ?",
            ((EVALUATED_AT - timedelta(minutes=30)).isoformat(), GENERATION_ID),
        )
        self.db.execute(
            "UPDATE signals SET timestamp = ? WHERE signal_id = ?",
            ((EVALUATED_AT - timedelta(minutes=30)).isoformat(), str(SIGNAL_ID)),
        )

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("STALE_SIGNAL", result["rejection_reason"])
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])

    def test_missing_stop_is_rejected(self) -> None:
        self.db.execute(
            "UPDATE signals SET payload = ? WHERE signal_id = ?",
            (to_json({
                "entry_price": 100.0,
                "stop_loss": None,
                "take_profit": 102.0,
                "reward_risk": 2.0,
                "expected_edge": 100.0,
                "regime": "TRENDING_UP",
                "reasons": [],
                "invalidation_conditions": [],
            }), str(SIGNAL_ID)),
        )

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("INVALID_STOP", result["rejection_reason"])
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])

    def test_signal_created_after_requested_risk_time_is_rejected(self) -> None:
        future = (EVALUATED_AT + timedelta(minutes=1)).isoformat()
        payload = {
            "provenance": {
                "run_id": RUN_ID,
                "instrument_id": INSTRUMENT.instrument_id,
                "strategy": "orb_vwap",
                "strategy_version": "1.0.0",
                "data_quality": "VALID",
                "evaluation_as_of": future,
            },
        }
        self.db.execute(
            "UPDATE generated_strategy_signals SET evaluation_as_of = ?, "
            "generated_at = ?, payload = ? WHERE generation_id = ?",
            (future, future, to_json(payload), GENERATION_ID),
        )

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("FUTURE_DATED_SIGNAL_CONTEXT", result["rejection_reason"])
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])

    def test_insufficient_cash_for_minimum_lot_is_rejected_by_sizing(self) -> None:
        constrained = replace(INSTRUMENT, minimum_order_quantity=10)
        self.service.instruments[constrained.instrument_id] = constrained
        self.service.portfolio = PortfolioManager("USD", 500.0)
        self.service.sizing_limits = SizingLimits(
            risk_per_trade_pct=0.50,
            max_order_notional=50_000.0,
            cash_requirement_rate=1.0,
        )

        result = self.service.evaluate(RUN_ID, constrained.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("POSITION_SIZING_REJECTED", result["rejection_reason"])
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])

    def test_portfolio_exposure_cap_can_reject_a_minimum_lot(self) -> None:
        constrained = replace(INSTRUMENT, minimum_order_quantity=20)
        self.service.instruments[constrained.instrument_id] = constrained
        self.service.sizing_limits = SizingLimits(
            risk_per_trade_pct=0.01,
            max_order_notional=50_000.0,
            cash_requirement_rate=1.0,
            max_total_notional_pct=0.001,
        )

        result = self.service.evaluate(RUN_ID, constrained.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("POSITION_SIZING_REJECTED", result["rejection_reason"])
        self.assertEqual(
            result["provenance"]["sizing"]["binding_constraint"],
            "portfolio_exposure",
        )
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])

    def test_hold_signal_is_non_actionable_and_unpersisted(self) -> None:
        self.db.execute(
            "UPDATE signals SET side = 'HOLD', payload = ? WHERE signal_id = ?",
            (to_json({
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "reward_risk": None,
                "expected_edge": 0.0,
                "regime": "UNKNOWN",
                "reasons": [],
                "invalidation_conditions": [],
            }), str(SIGNAL_ID)),
        )

        result = self.service.evaluate(RUN_ID, INSTRUMENT.instrument_id)

        self.assertEqual(result["status"], "NON_ACTIONABLE")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        self.assertEqual(self.db.query("SELECT * FROM risk_evaluations"), [])
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])

    def test_missing_fx_is_persisted_as_rejection(self) -> None:
        instrument = replace(INSTRUMENT, currency="EUR")
        self.service.instruments[instrument.instrument_id] = instrument

        result = self.service.evaluate(RUN_ID, instrument.instrument_id)

        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertIn("MISSING_FX", result["rejection_reason"])
        self.assertEqual(self.db.query("SELECT * FROM trade_proposals"), [])


if __name__ == "__main__":
    unittest.main()
