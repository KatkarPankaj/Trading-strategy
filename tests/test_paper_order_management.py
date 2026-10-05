from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import unittest
from uuid import uuid4

from stockmarket.core import (
    AssetClass,
    Instrument,
    InvalidOrderTransition,
    Order,
    OrderIntent,
    OrderManager,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperAccountingMode,
    PaperAction,
    PaperExecutionPolicy,
    PaperExecutor,
    PaperPortfolio,
    PositionSide,
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskEngine,
    RiskLimits,
    Signal,
    SignalSide,
    TradingStatus,
)


NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
DATA_AT = NOW - timedelta(seconds=20)


class PaperOrderManagementTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument(
            instrument_id="XNSE:RELIANCE",
            symbol="RELIANCE.NS",
            exchange="NSE",
            market="IN",
            asset_class=AssetClass.EQUITY,
            currency="INR",
            timezone="Asia/Kolkata",
            tick_size=0.05,
            shortable=True,
            trading_status=TradingStatus.ACTIVE,
        )
        self.signal = Signal(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol,
            timestamp=DATA_AT,
            strategy="ORB + VWAP",
            side=SignalSide.BUY,
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            expected_edge=100.0,
        )
        self.context = RiskContext(
            assessed_at=NOW,
            market_data_timestamp=DATA_AT,
            market_data_valid=True,
            reference_price=100.0,
            available_cash=10000.0,
            estimated_fees=10.0,
            current_position_quantity=0,
            current_position_side=None,
            open_positions=0,
            trades_today=0,
            stop_loss=99.0,
            take_profit=102.0,
            signal=self.signal,
        )
        self.limits = RiskLimits(
            max_position_quantity=200,
            max_order_notional=50000,
            max_open_positions=3,
            max_trades_per_day=10,
            cash_requirement_rate=0.20,
            entry_window=(time(9, 15), time(15, 30)),
            minimum_reward_risk=1.0,
            minimum_expected_edge=40,
            max_market_data_age=timedelta(minutes=2),
        )
        self.risk = RiskEngine(self.limits)
        self.executor = PaperExecutor(
            PaperExecutionPolicy(
                accounting_mode=PaperAccountingMode.MARGIN_LONG_ONLY
            )
        )
        self.manager = OrderManager(self.risk, self.executor)
        self.portfolio = PaperPortfolio(starting_cash=10000.0)

    def make_order(self, **overrides):
        values = {
            "instrument_id": self.instrument.instrument_id,
            "symbol": self.instrument.symbol,
            "side": OrderSide.BUY,
            "quantity": 10,
            "order_type": OrderType.MARKET,
            "created_at": NOW,
            "signal_id": self.signal.signal_id,
        }
        values.update(overrides)
        return Order(**values)

    def submit(self, order=None, context=None, portfolio=None, **kwargs):
        fill_price = kwargs.pop("fill_price", 100.0)
        return self.manager.submit(
            order or self.make_order(),
            self.instrument,
            portfolio or self.portfolio,
            context or self.context,
            fill_price=fill_price,
            filled_at=NOW,
            note="unit test paper fill",
            **kwargs,
        )

    @staticmethod
    def approved(order):
        return RiskDecision(
            status=RiskDecisionStatus.APPROVED,
            timestamp=NOW,
            order_id=order.order_id,
        )

    def test_valid_order_runs_risk_then_fills_and_updates_portfolio(self):
        result = self.submit()
        self.assertEqual(result.risk_decision.status,
                         RiskDecisionStatus.APPROVED)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.fill.action, PaperAction.BUY)
        self.assertEqual(result.fill.quantity, 10)
        self.assertEqual(
            self.portfolio.positions[self.instrument.instrument_id].quantity, 10)
        self.assertAlmostEqual(self.portfolio.cash,
                               10000.0 - 200.0 - result.fill.charges)
        self.assertEqual(len(self.portfolio.fills), 1)

    def test_risk_rejection_does_not_call_executor_or_mutate_portfolio(self):
        self.portfolio.cash = 0.0
        before = (
            self.portfolio.cash,
            dict(self.portfolio.positions),
            list(self.portfolio.fills),
        )
        result = self.submit(context=replace(
            self.context, available_cash=10000.0))
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertEqual(result.risk_decision.status,
                         RiskDecisionStatus.REJECTED)
        self.assertIsNone(result.fill)
        self.assertEqual(
            before,
            (self.portfolio.cash, dict(self.portfolio.positions),
             list(self.portfolio.fills)),
        )

    def test_duplicate_submission_returns_original_result_without_second_fill(self):
        order = self.make_order()
        original = self.submit(order=order)
        retry = self.submit(order=order)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.order.order_id, original.order.order_id)
        self.assertEqual(len(self.portfolio.fills), 1)
        self.assertEqual(
            self.portfolio.positions[self.instrument.instrument_id].quantity, 10)

    def test_reusing_order_id_for_different_request_is_rejected(self):
        original_order = self.make_order()
        self.submit(order=original_order)
        conflicting_order = replace(original_order, quantity=11)
        conflict = self.submit(order=conflicting_order)
        self.assertTrue(conflict.duplicate)
        self.assertEqual(conflict.risk_decision.status,
                         RiskDecisionStatus.REJECTED)
        self.assertTrue(conflict.error.startswith("IDEMPOTENCY_KEY_REUSED:"))
        self.assertEqual(len(self.portfolio.fills), 1)

    def test_invalid_transition_and_cancelled_order_do_not_fill(self):
        rejected_context = replace(self.context, market_data_valid=False)
        rejected = self.submit(order=self.make_order(),
                               context=rejected_context)
        with self.assertRaises(InvalidOrderTransition):
            self.manager.transition(
                rejected.order.order_id, OrderStatus.FILLED)
        with self.assertRaises(InvalidOrderTransition):
            self.manager.transition(
                rejected.order.order_id, OrderStatus.SUBMITTED)
        self.assertEqual(self.portfolio.fills, [])

        cancellable = self.make_order(order_id=uuid4())
        staged = self.manager.stage(
            cancellable,
            self.instrument,
            self.portfolio,
            self.context,
        )
        self.assertEqual(staged.status, OrderStatus.VALIDATED)
        cancelled = self.manager.cancel(cancellable.order_id)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        with self.assertRaises(InvalidOrderTransition):
            self.manager.transition(cancellable.order_id, OrderStatus.FILLED)
        self.assertEqual(self.portfolio.fills, [])

    def test_staged_order_can_be_submitted_once_after_revalidation(self):
        order = self.make_order(order_id=uuid4())
        staged = self.manager.stage(
            order, self.instrument, self.portfolio, self.context
        )
        self.assertEqual(staged.status, OrderStatus.VALIDATED)
        result = self.submit(order=order)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(len(self.portfolio.fills), 1)

    def test_paper_executor_uses_full_cash_policy_and_short_cover_actions(self):
        portfolio = PaperPortfolio(starting_cash=10000)
        executor = PaperExecutor(
            PaperExecutionPolicy(
                accounting_mode=PaperAccountingMode.CASH_LONG_SHORT,
                allow_averaging=True,
            )
        )
        long_order = self.make_order()
        long_fill = executor.execute(
            long_order, self.instrument, portfolio,
            risk_decision=self.approved(long_order),
            fill_price=100, intent=OrderIntent.ENTRY, filled_at=NOW,
            stop_loss=99, take_profit=102,
        )
        self.assertEqual(long_fill.action, PaperAction.BUY)
        self.assertAlmostEqual(portfolio.cash, 10000 -
                               1000 - long_fill.charges)

        # Close long before opening a short in the same instrument.
        exit_order = self.make_order(
            side=OrderSide.SELL, signal_id=None, order_id=uuid4()
        )
        exit_fill = executor.execute(
            exit_order, self.instrument, portfolio,
            risk_decision=self.approved(exit_order),
            fill_price=101, intent=OrderIntent.EXIT, filled_at=NOW,
        )
        self.assertEqual(exit_fill.action, PaperAction.SELL)
        self.assertAlmostEqual(exit_fill.realized_pnl, 10.0)

        short_order = self.make_order(
            side=OrderSide.SELL, signal_id=None, order_id=uuid4()
        )
        short_fill = executor.execute(
            short_order, self.instrument, portfolio,
            risk_decision=self.approved(short_order),
            fill_price=100, intent=OrderIntent.ENTRY, filled_at=NOW,
            stop_loss=101, take_profit=98,
        )
        self.assertEqual(short_fill.action, PaperAction.SHORT)
        self.assertEqual(
            portfolio.positions[self.instrument.instrument_id].side, PositionSide.SHORT)

        cover_order = self.make_order(order_id=uuid4())
        cover_fill = executor.execute(
            cover_order, self.instrument, portfolio,
            risk_decision=self.approved(cover_order),
            fill_price=99, intent=OrderIntent.EXIT, filled_at=NOW,
        )
        self.assertEqual(cover_fill.action, PaperAction.COVER)
        self.assertAlmostEqual(cover_fill.realized_pnl, 10.0)
        self.assertNotIn(self.instrument.instrument_id, portfolio.positions)

    def test_execution_failure_leaves_portfolio_unchanged(self):
        before_cash = self.portfolio.cash
        result = self.submit(fill_price=100000)
        self.assertEqual(result.risk_decision.status,
                         RiskDecisionStatus.APPROVED)
        self.assertEqual(result.status, OrderStatus.FAILED)
        self.assertIsNone(result.fill)
        self.assertEqual(self.portfolio.cash, before_cash)
        self.assertEqual(self.portfolio.positions, {})
        self.assertEqual(self.portfolio.fills, [])


if __name__ == "__main__":
    unittest.main()
