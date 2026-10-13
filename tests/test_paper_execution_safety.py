from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from stockmarket.core.brokers import BrokerError, BrokerRejected, PaperBroker
from stockmarket.core.models import OrderSide, OrderStatus, OrderType, TradeIntent
from stockmarket.core.order_management import ManagedOrder
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.portfolio import PortfolioManager

from test_trade_intent import NOW, make_instrument


class ExecutionReservationTests(TestCase):
    def test_separate_database_connections_cannot_acquire_same_account(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.db")
            first, second = SQLiteDatabase(path), SQLiteDatabase(path)
            try:
                migrate(first)
                one, two = Store(first), Store(second)
                one.execution_reservations.acquire("PAPER", "first-worker", NOW)
                for at in (NOW, NOW + timedelta(minutes=6)):
                    with self.assertRaisesRegex(ValueError, "EXECUTION_RESERVED"):
                        two.execution_reservations.acquire("PAPER", "second-worker", at)
                with self.assertRaisesRegex(ValueError, "EXECUTION_RESERVED"):
                    two.execution_reservations.recover(NOW)
                one.execution_reservations.finish("PAPER", "not-owner", failed=False)
                with self.assertRaises(ValueError):
                    two.execution_reservations.acquire("PAPER", "second-worker", NOW)
                self.assertEqual(two.execution_reservations.recover(
                    NOW + timedelta(minutes=6)), ["PAPER"])
                two.execution_reservations.acquire("PAPER", "second-worker", NOW + timedelta(minutes=6))
                one.execution_reservations.finish("PAPER", "first-worker", failed=False)
                self.assertEqual(first.query("SELECT owner_id FROM execution_reservations"),
                                 [{"owner_id": "second-worker"}])
            finally:
                first.close()
                second.close()


class PaperQuoteBoundaryTests(TestCase):
    def setUp(self):
        self.at = NOW
        self.instrument = make_instrument()
        self.portfolio = PortfolioManager("USD", 10_000)
        self.broker = PaperBroker(self.portfolio, {self.instrument.instrument_id: self.instrument},
                                  clock=lambda: self.at)
        self.broker.connect()
        self.broker.update_price(self.instrument.instrument_id, 100.0, NOW)

    def order(self, **values):
        order = ManagedOrder(
            client_order_id="entry", broker_order_id=None, instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol, side=OrderSide.BUY, quantity=10,
            order_type=OrderType.MARKET, limit_price=None, stop_price=None,
            timestamp=self.at, strategy="fixture", signal_id=None, risk_decision_id=None,
            status=OrderStatus.SUBMITTED, trade_intent=TradeIntent.BUY)
        return replace(order, **values)

    def test_stale_cached_quote_cannot_fill_new_market_order(self):
        self.at += timedelta(seconds=61)
        with self.assertRaisesRegex(BrokerRejected, "stale"):
            self.broker.submit_order(self.order())
        self.assertEqual(self.portfolio.fills, ())
        self.assertEqual(self.portfolio.cash["USD"], 10_000)

    def test_invalid_quote_updates_cannot_mark_or_fill_resting_order(self):
        broker_id = self.broker.submit_order(self.order(
            order_type=OrderType.LIMIT, limit_price=99.0))
        self.at += timedelta(minutes=2)
        for timestamp in (NOW, self.at + timedelta(seconds=1), self.at.replace(tzinfo=None)):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(BrokerError):
                    self.broker.update_price(self.instrument.instrument_id, 98.0, timestamp)
                self.assertEqual(self.broker.order_status(broker_id).filled_quantity, 0)
                self.assertEqual(self.portfolio.fills, ())
        self.broker.modify_order(broker_id, limit_price=101.0)
        self.assertEqual(self.portfolio.fills, ())
        self.broker.update_price(self.instrument.instrument_id, 98.0, self.at)
        self.assertEqual(self.broker.order_status(broker_id).filled_quantity, 10)
        self.assertEqual(len(self.portfolio.fills), 1)

    def test_long_and_short_cash_fees_and_realized_pnl(self):
        for side, entry_intent, exit_intent, exit_price, expected_cash in (
            (OrderSide.BUY, TradeIntent.BUY, TradeIntent.SELL_TO_CLOSE, 102.0, 10_017.98),
            (OrderSide.SELL, TradeIntent.SHORT_SELL, TradeIntent.COVER, 98.0, 10_018.02),
        ):
            with self.subTest(intent=entry_intent):
                portfolio = PortfolioManager("USD", 10_000)
                broker = PaperBroker(portfolio, {self.instrument.instrument_id: self.instrument},
                                     fee_rate=0.001, clock=lambda: NOW)
                broker.connect()
                broker.update_price(self.instrument.instrument_id, 100.0, NOW)
                broker.submit_order(self.order(side=side, trade_intent=entry_intent))
                signed = 1 if side is OrderSide.BUY else -1
                self.assertAlmostEqual(portfolio.cash["USD"], 10_000 - signed * 1000 - 1)
                broker.update_price(self.instrument.instrument_id, exit_price, NOW)
                broker.submit_order(self.order(
                    client_order_id="close", side=exit_intent.order_side, trade_intent=exit_intent))
                self.assertEqual(portfolio.positions(), {})
                self.assertEqual(len(portfolio.fills), 2)
                self.assertAlmostEqual(portfolio.cash["USD"], expected_cash)
                self.assertAlmostEqual(portfolio.realized_pnl, 20.0)
                self.assertAlmostEqual(portfolio.fees, 1 + exit_price * 10 * 0.001)
                self.assertAlmostEqual(portfolio.realized_pnl - portfolio.fees, expected_cash - 10_000)

    def test_resting_fill_and_executor_state_roll_back_together_on_persistence_failure(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        migrate(database)
        store = Store(database)
        store.instruments.save(self.instrument)

        def interrupted(fill):
            store.fills.save(fill)
            raise RuntimeError("injected persistence interruption")

        self.portfolio.on_fill = interrupted
        broker = PaperBroker(
            self.portfolio, {self.instrument.instrument_id: self.instrument},
            clock=lambda: self.at, execution_repository=store.paper_execution,
            fills_repository=store.fills)
        broker.connect()
        broker.update_price(self.instrument.instrument_id, 100.0, self.at)
        broker_id = broker.submit_order(self.order(order_type=OrderType.LIMIT, limit_price=99.0))
        with self.assertRaisesRegex(RuntimeError, "persistence interruption"):
            broker.update_price(self.instrument.instrument_id, 98.0, self.at)
        self.assertEqual(self.portfolio.cash["USD"], 10_000)
        self.assertEqual(self.portfolio.fills, ())
        self.assertEqual(self.portfolio.positions(), {})
        self.assertEqual(store.fills.all(), [])
        self.assertEqual(store.paper_execution.get_by_broker_order_id(broker_id)["status"], "ACCEPTED")
        self.assertEqual(broker.order_status(broker_id).filled_quantity, 0)
        self.portfolio.on_fill = store.fills.save
        broker.update_price(self.instrument.instrument_id, 98.0, self.at)
        broker.update_price(self.instrument.instrument_id, 97.0, self.at)
        self.assertEqual(len(store.fills.all()), 1)
        self.assertEqual(len(self.portfolio.fills), 1)
        self.assertEqual(broker.order_status(broker_id).filled_quantity, 10)
