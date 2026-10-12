"""Explicit trade intent: BUY / SELL_TO_CLOSE / SHORT_SELL / COVER / NO_TRADE."""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import unittest

from pydantic import ValidationError

from stockmarket.api.schemas import OrderBody
from stockmarket.core import (
    AssetClass,
    Instrument,
    MarketSession,
    Order,
    OrderIntent,
    OrderSide,
    OrderType,
    PositionSide,
    RiskContext,
    RiskDecisionStatus,
    RiskEngine,
    RiskLimits,
    Signal,
    SignalSide,
    TradeIntent,
    TradingStatus,
)
from stockmarket.core.brokers import PaperBroker
from stockmarket.core.executors import TradingMode
from stockmarket.core.models import coerce_trade_intent
from stockmarket.core.order_management import (
    IdempotencyConflict,
    OrderManager,
    OrderRequest,
)
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.recovery import order_from_row
from stockmarket.core.trading_service import (
    AmbiguousTradeIntent,
    OrderTicket,
    TradingService,
    ticket_trade_intent,
)


NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
DATA_AT = NOW - timedelta(seconds=30)
ZONE = "America/New_York"


def make_instrument(*, shortable=True, instrument_id="XNAS:AAPL", symbol="AAPL"):
    return Instrument(
        instrument_id=instrument_id,
        symbol=symbol,
        exchange="XNAS",
        market="US",
        asset_class=AssetClass.EQUITY,
        currency="USD",
        timezone=ZONE,
        tick_size=0.01,
        shortable=shortable,
        trading_status=TradingStatus.ACTIVE,
    )


def make_session():
    return MarketSession(
        timezone=ZONE,
        market_open=time(9, 30),
        opening_range_end=time(9, 45),
        entry_cutoff=time(15, 0),
        square_off=time(15, 55),
        market_close=time(16, 0),
        late_entry_start=time(12, 0),
    )


def limits():
    return RiskLimits(
        max_position_quantity=100,
        max_order_notional=20_000,
        max_open_positions=3,
        max_trades_per_day=10,
        cash_requirement_rate=1.0,
        market_session=make_session(),
    )


class TradeIntentModelTests(unittest.TestCase):
    def test_intent_mapping(self):
        self.assertIs(TradeIntent.BUY.order_side, OrderSide.BUY)
        self.assertIs(TradeIntent.COVER.order_side, OrderSide.BUY)
        self.assertIs(TradeIntent.SELL_TO_CLOSE.order_side, OrderSide.SELL)
        self.assertIs(TradeIntent.SHORT_SELL.order_side, OrderSide.SELL)
        self.assertIsNone(TradeIntent.NO_TRADE.order_side)
        self.assertEqual(
            {i for i in TradeIntent if i.is_entry}, {TradeIntent.BUY, TradeIntent.SHORT_SELL})
        self.assertEqual(
            {i for i in TradeIntent if i.is_exit},
            {TradeIntent.SELL_TO_CLOSE, TradeIntent.COVER})
        self.assertFalse(TradeIntent.NO_TRADE.is_executable)
        self.assertIs(TradeIntent.close_for(PositionSide.LONG), TradeIntent.SELL_TO_CLOSE)
        self.assertIs(TradeIntent.close_for(PositionSide.SHORT), TradeIntent.COVER)
        self.assertIs(TradeIntent.entry_for(OrderSide.SELL), TradeIntent.SHORT_SELL)
        self.assertIs(coerce_trade_intent(" short_sell "), TradeIntent.SHORT_SELL)
        with self.assertRaises(ValueError):
            coerce_trade_intent("SELL")

    def signal(self, side, intent=None):
        return Signal(
            instrument_id="XNAS:AAPL", symbol="AAPL", timestamp=DATA_AT, strategy="s",
            side=side, entry_price=100.0 if side is not SignalSide.HOLD else None,
            intent=intent)

    def test_signal_intent_is_explicit_for_sell(self):
        self.assertIs(self.signal(SignalSide.BUY).intent, TradeIntent.BUY)
        self.assertIs(self.signal(SignalSide.HOLD).intent, TradeIntent.NO_TRADE)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.signal(SignalSide.SELL)
        self.assertIs(
            self.signal(SignalSide.SELL, TradeIntent.SELL_TO_CLOSE).intent,
            TradeIntent.SELL_TO_CLOSE)
        with self.assertRaisesRegex(ValueError, "contradicts"):
            self.signal(SignalSide.BUY, TradeIntent.SHORT_SELL)
        with self.assertRaises(ValueError):
            self.signal(SignalSide.HOLD, TradeIntent.BUY)

    def test_order_rejects_no_trade_and_side_mismatch(self):
        base = dict(instrument_id="XNAS:AAPL", symbol="AAPL", quantity=1,
                    order_type=OrderType.MARKET, created_at=NOW)
        with self.assertRaisesRegex(ValueError, "NO_TRADE"):
            Order(side=OrderSide.BUY, intent=TradeIntent.NO_TRADE, **base)
        with self.assertRaisesRegex(ValueError, "contradicts"):
            Order(side=OrderSide.BUY, intent=TradeIntent.SELL_TO_CLOSE, **base)
        self.assertIsNone(Order(side=OrderSide.SELL, **base).intent)

    def test_ticket_intent_never_guesses_a_sell_entry(self):
        base = dict(instrument_id="XNAS:AAPL", quantity=1, order_type=OrderType.MARKET,
                    strategy="manual")
        self.assertIs(ticket_trade_intent(OrderTicket(side=OrderSide.BUY, **base)),
                      TradeIntent.BUY)
        with self.assertRaises(AmbiguousTradeIntent):
            ticket_trade_intent(OrderTicket(side=OrderSide.SELL, **base))
        self.assertIs(ticket_trade_intent(OrderTicket(
            side=OrderSide.SELL, intent=OrderIntent.EXIT, **base)), TradeIntent.SELL_TO_CLOSE)
        self.assertIs(ticket_trade_intent(OrderTicket(
            side=OrderSide.BUY, intent=OrderIntent.EXIT, **base)), TradeIntent.COVER)


class RiskEngineIntentTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()
        self.engine = RiskEngine(limits())

    def evaluate(self, side, *, intent=None, purpose=OrderIntent.ENTRY, position=0,
                 position_side=None, quantity=10, trade_intent=None, signal=None,
                 instrument=None):
        order = Order(
            instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol,
            side=side, quantity=quantity, order_type=OrderType.MARKET, created_at=NOW,
            intent=intent, signal_id=signal.signal_id if signal else None)
        context = RiskContext(
            assessed_at=NOW, market_data_timestamp=DATA_AT, market_data_valid=True,
            reference_price=100.0, available_cash=10_000.0, estimated_fees=1.0,
            current_position_quantity=position, current_position_side=position_side,
            open_positions=1 if position else 0, trades_today=0, intent=purpose,
            stop_loss=(99.0 if side is OrderSide.BUY else 101.0)
            if purpose is OrderIntent.ENTRY else None,
            take_profit=(102.0 if side is OrderSide.BUY else 98.0)
            if purpose is OrderIntent.ENTRY else None,
            trade_intent=trade_intent, signal=signal)
        return self.engine.evaluate(order, instrument or self.instrument, context)

    def assert_rejected(self, decision, code):
        self.assertIs(decision.status, RiskDecisionStatus.REJECTED, decision.reason)
        self.assertTrue(decision.reason.startswith(code + ":"), decision.reason)

    def test_each_executable_intent_approved_in_valid_state(self):
        cases = (
            (OrderSide.BUY, TradeIntent.BUY, OrderIntent.ENTRY, 0, None),
            (OrderSide.SELL, TradeIntent.SHORT_SELL, OrderIntent.ENTRY, 0, None),
            (OrderSide.SELL, TradeIntent.SELL_TO_CLOSE, OrderIntent.EXIT, 10,
             PositionSide.LONG),
            (OrderSide.BUY, TradeIntent.COVER, OrderIntent.EXIT, 10, PositionSide.SHORT),
        )
        for side, intent, purpose, qty, pos_side in cases:
            with self.subTest(intent=intent):
                decision = self.evaluate(
                    side, intent=intent, purpose=purpose, position=qty,
                    position_side=pos_side)
                self.assertIs(decision.status, RiskDecisionStatus.APPROVED, decision.reason)

    def test_no_trade_is_never_approved(self):
        decision = self.evaluate(OrderSide.BUY, trade_intent=TradeIntent.NO_TRADE)
        self.assert_rejected(decision, "NO_TRADE_NOT_EXECUTABLE")

    def test_exit_without_matching_position_is_rejected(self):
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SELL_TO_CLOSE,
                          purpose=OrderIntent.EXIT),
            "EXIT_QUANTITY_EXCEEDS_POSITION")
        self.assert_rejected(
            self.evaluate(OrderSide.BUY, intent=TradeIntent.COVER, purpose=OrderIntent.EXIT,
                          position=10, position_side=PositionSide.LONG),
            "EXIT_SIDE_CONTRADICTS_POSITION")

    def test_insufficient_holdings_is_rejected(self):
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SELL_TO_CLOSE,
                          purpose=OrderIntent.EXIT, position=5,
                          position_side=PositionSide.LONG, quantity=6),
            "EXIT_QUANTITY_EXCEEDS_POSITION")

    def test_unshortable_short_sell_is_rejected(self):
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SHORT_SELL,
                          instrument=make_instrument(shortable=None)),
            "SHORTABILITY_UNCONFIRMED")

    def test_contradictory_intents_are_rejected(self):
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SHORT_SELL,
                          purpose=OrderIntent.EXIT, position=10,
                          position_side=PositionSide.LONG),
            "TRADE_INTENT_CONTRADICTORY")
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SHORT_SELL,
                          trade_intent=TradeIntent.SELL_TO_CLOSE),
            "TRADE_INTENT_CONTRADICTORY")

    def test_signal_intent_must_match_order_intent(self):
        signal = Signal(
            instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol,
            timestamp=DATA_AT, strategy="s", side=SignalSide.SELL, entry_price=100.0,
            stop_loss=101.0, take_profit=98.0, intent=TradeIntent.SELL_TO_CLOSE)
        self.assert_rejected(
            self.evaluate(OrderSide.SELL, intent=TradeIntent.SHORT_SELL, signal=signal),
            "SIGNAL_INTENT_MISMATCH")

    def test_evaluate_proposal_carries_intent(self):
        decision = self.engine.evaluate_proposal(
            self.instrument, side=OrderSide.SELL, quantity=5, order_type=OrderType.MARKET,
            created_at=NOW, intent=TradeIntent.SELL_TO_CLOSE,
            context=RiskContext(
                assessed_at=NOW, market_data_timestamp=DATA_AT, market_data_valid=True,
                reference_price=100.0, available_cash=0.0, estimated_fees=0.0,
                current_position_quantity=5, current_position_side=PositionSide.LONG,
                open_positions=1, trades_today=0, intent=OrderIntent.EXIT))
        self.assertIs(decision.status, RiskDecisionStatus.APPROVED, decision.reason)


class OrderRequestIntentTests(unittest.TestCase):
    def request(self, **overrides):
        values = dict(
            client_order_id="cid-1", instrument_id="XNAS:AAPL", symbol="AAPL",
            side=OrderSide.SELL, quantity=1, order_type=OrderType.MARKET, timestamp=NOW,
            strategy="s", trade_intent=TradeIntent.SELL_TO_CLOSE)
        values.update(overrides)
        return OrderRequest(**values)

    def test_same_request_distinguishes_intent_but_tolerates_legacy_null(self):
        close = self.request()
        self.assertTrue(close.same_request(self.request()))
        self.assertFalse(close.same_request(
            self.request(trade_intent=TradeIntent.SHORT_SELL)))
        self.assertTrue(close.same_request(self.request(trade_intent=None)))


class TradingServiceIntentTests(unittest.TestCase):
    """Real PaperBroker/OrderManager/PortfolioManager with SQLite persistence."""

    def setUp(self):
        self.instrument = make_instrument()
        self.unshortable = make_instrument(
            shortable=None, instrument_id="XNAS:MSFT", symbol="MSFT")
        instruments = {
            self.instrument.instrument_id: self.instrument,
            self.unshortable.instrument_id: self.unshortable,
        }
        self.db = SQLiteDatabase()
        migrate(self.db)
        self.store = Store(self.db)
        self.portfolio = PortfolioManager("USD", 50_000, timezone=ZONE)
        self.portfolio.on_fill = self.store.fills.save
        self.broker = PaperBroker(
            self.portfolio, instruments, clock=lambda: NOW, market_status_fn=lambda _: True)
        self.broker.connect()
        for iid in instruments:
            self.broker.update_price(iid, 100.0, DATA_AT)
        self.orders = OrderManager(self.broker, clock=lambda: NOW)
        self.service = TradingService(
            mode=TradingMode.PAPER, risk_engine=RiskEngine(limits()),
            order_manager=self.orders, portfolio=self.portfolio, instruments=instruments,
            quotes=self.broker.last_quote, store=self.store, clock=lambda: NOW)

    def ticket(self, side, *, purpose=OrderIntent.ENTRY, trade_intent=None, qty=10,
               cid=None, instrument_id=None):
        entry = purpose is OrderIntent.ENTRY
        return OrderTicket(
            instrument_id=instrument_id or self.instrument.instrument_id, side=side,
            quantity=qty, order_type=OrderType.MARKET, strategy="manual",
            client_order_id=cid, intent=purpose, trade_intent=trade_intent,
            stop_loss=(99.0 if side is OrderSide.BUY else 101.0) if entry else None,
            take_profit=(102.0 if side is OrderSide.BUY else 98.0) if entry else None)

    def snapshot(self):
        return (
            len(self.db.query("SELECT * FROM orders")),
            len(self.db.query("SELECT * FROM fills")),
            dict(self.portfolio.cash),
            {k: v.quantity for k, v in self.portfolio.positions().items()},
        )

    def test_sell_entry_without_intent_is_refused_before_any_mutation(self):
        before = self.snapshot()
        with self.assertRaises(AmbiguousTradeIntent):
            self.service.submit(self.ticket(OrderSide.SELL))
        self.assertEqual(self.snapshot(), before)

    def test_buy_then_sell_to_close_round_trip_persists_intent(self):
        opened = self.service.submit(self.ticket(OrderSide.BUY, cid="open-1"))
        self.assertIs(opened.risk.status, RiskDecisionStatus.APPROVED, opened.risk.reason)
        self.assertIs(opened.order.trade_intent, TradeIntent.BUY)
        closed = self.service.submit(self.ticket(
            OrderSide.SELL, purpose=OrderIntent.EXIT,
            trade_intent=TradeIntent.SELL_TO_CLOSE, cid="close-1"))
        self.assertIs(closed.risk.status, RiskDecisionStatus.APPROVED, closed.risk.reason)
        self.assertEqual(self.portfolio.positions(), {})
        rows = {row["client_order_id"]: row for row in self.db.query(
            "SELECT * FROM orders")}
        self.assertEqual(rows["open-1"]["trade_intent"], "BUY")
        self.assertEqual(rows["close-1"]["trade_intent"], "SELL_TO_CLOSE")
        self.assertIs(order_from_row(rows["close-1"]).trade_intent, TradeIntent.SELL_TO_CLOSE)

    def test_short_sell_and_cover_round_trip(self):
        opened = self.service.submit(self.ticket(
            OrderSide.SELL, trade_intent=TradeIntent.SHORT_SELL, cid="short-1"))
        self.assertIs(opened.risk.status, RiskDecisionStatus.APPROVED, opened.risk.reason)
        position = self.portfolio.positions()[self.instrument.instrument_id]
        self.assertIs(position.side, PositionSide.SHORT)
        covered = self.service.submit(self.ticket(
            OrderSide.BUY, purpose=OrderIntent.EXIT, trade_intent=TradeIntent.COVER,
            cid="cover-1"))
        self.assertIs(covered.risk.status, RiskDecisionStatus.APPROVED, covered.risk.reason)
        self.assertEqual(self.portfolio.positions(), {})

    def test_invalid_transitions_are_rejected_without_portfolio_mutation(self):
        cases = (
            ("close-without-long", self.ticket(
                OrderSide.SELL, purpose=OrderIntent.EXIT,
                trade_intent=TradeIntent.SELL_TO_CLOSE, cid="close-without-long")),
            ("cover-without-short", self.ticket(
                OrderSide.BUY, purpose=OrderIntent.EXIT, trade_intent=TradeIntent.COVER,
                cid="cover-without-short")),
            ("unshortable", self.ticket(
                OrderSide.SELL, trade_intent=TradeIntent.SHORT_SELL, cid="unshortable",
                instrument_id=self.unshortable.instrument_id)),
        )
        cash = dict(self.portfolio.cash)
        for name, ticket in cases:
            with self.subTest(name):
                result = self.service.submit(ticket)
                self.assertIs(result.risk.status, RiskDecisionStatus.REJECTED)
        self.assertEqual(self.db.query("SELECT * FROM fills"), [])
        self.assertEqual(self.portfolio.positions(), {})
        self.assertEqual(dict(self.portfolio.cash), cash)

    def test_insufficient_holdings_rejects_oversized_close(self):
        self.service.submit(self.ticket(OrderSide.BUY, qty=5, cid="open-small"))
        result = self.service.submit(self.ticket(
            OrderSide.SELL, purpose=OrderIntent.EXIT, trade_intent=TradeIntent.SELL_TO_CLOSE,
            qty=6, cid="close-too-many"))
        self.assertIs(result.risk.status, RiskDecisionStatus.REJECTED)
        self.assertIn("EXIT_QUANTITY_EXCEEDS_POSITION", result.risk.reason)
        self.assertEqual(
            self.portfolio.positions()[self.instrument.instrument_id].quantity, 5)

    def test_duplicate_submission_is_idempotent_and_intent_change_conflicts(self):
        first = self.service.submit(self.ticket(OrderSide.BUY, cid="dup-1"))
        second = self.service.submit(self.ticket(OrderSide.BUY, cid="dup-1"))
        self.assertTrue(second.duplicate)
        self.assertEqual(first.order.client_order_id, second.order.client_order_id)
        self.assertEqual(len(self.db.query("SELECT * FROM fills")), 1)
        with self.assertRaises(IdempotencyConflict):
            self.service.submit(self.ticket(
                OrderSide.BUY, purpose=OrderIntent.EXIT, trade_intent=TradeIntent.COVER,
                cid="dup-1"))
        self.assertEqual(len(self.db.query("SELECT * FROM fills")), 1)

    def test_ticket_and_signal_intents_must_agree(self):
        signal = Signal(
            instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol,
            timestamp=DATA_AT, strategy="manual", side=SignalSide.SELL, entry_price=100.0,
            stop_loss=101.0, take_profit=98.0, intent=TradeIntent.SHORT_SELL)
        ticket = replace(
            self.ticket(OrderSide.SELL, trade_intent=TradeIntent.SELL_TO_CLOSE),
            signal=signal, signal_id=signal.signal_id)
        with self.assertRaises(AmbiguousTradeIntent):
            self.service.submit(ticket)
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])

    def test_submit_signal_refuses_exit_and_no_trade_signals(self):
        exit_signal = Signal(
            instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol,
            timestamp=DATA_AT, strategy="manual", side=SignalSide.SELL, entry_price=100.0,
            stop_loss=101.0, intent=TradeIntent.SELL_TO_CLOSE)
        with self.assertRaises(ValueError):
            self.service.submit_signal(exit_signal, 1)
        hold = Signal(
            instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol,
            timestamp=DATA_AT, strategy="manual", side=SignalSide.HOLD)
        with self.assertRaises(ValueError):
            self.service.submit_signal(hold, 1)
        self.assertEqual(self.db.query("SELECT * FROM orders"), [])


class ApiOrderBodyIntentTests(unittest.TestCase):
    def body(self, **overrides):
        values = dict(instrument_id="XNAS:AAPL", side="SELL", quantity=1,
                      order_type="MARKET", strategy="manual", stop_loss=101.0,
                      take_profit=98.0)
        values.update(overrides)
        return OrderBody(**values)

    def test_sell_entry_requires_explicit_intent(self):
        with self.assertRaises(ValidationError):
            self.body()
        self.assertIs(self.body(trade_intent="SHORT_SELL").trade_intent,
                      TradeIntent.SHORT_SELL)

    def test_no_trade_and_contradictions_are_rejected(self):
        for overrides in (
            {"trade_intent": "NO_TRADE"},
            {"trade_intent": "BUY"},
            {"trade_intent": "SELL_TO_CLOSE"},
            {"side": "BUY", "trade_intent": "COVER"},
        ):
            with self.subTest(overrides), self.assertRaises(ValidationError):
                self.body(**overrides)


if __name__ == "__main__":
    unittest.main()
