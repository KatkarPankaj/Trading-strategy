from datetime import datetime, time, timezone
import unittest
from uuid import UUID

from stockmarket.core import (
    AssetClass,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    RiskDecision,
    RiskDecisionStatus,
    Signal,
    SignalSide,
    TradingStatus,
)


UTC_NOW = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


class InstrumentTests(unittest.TestCase):
    def make_instrument(self, **overrides):
        values = {
            "instrument_id": "XNSE:RELIANCE",
            "symbol": "RELIANCE.NS",
            "exchange": "NSE",
            "market": "IN",
            "asset_class": AssetClass.EQUITY,
            "currency": "INR",
            "timezone": "Asia/Kolkata",
            "tick_size": 0.05,
            "trading_hours": (time(9, 15), time(15, 30)),
        }
        values.update(overrides)
        return Instrument(**values)

    def test_accepts_instrument_and_defaults(self):
        instrument = self.make_instrument()
        self.assertEqual(instrument.instrument_id, "XNSE:RELIANCE")
        self.assertEqual(instrument.lot_size, 1)
        self.assertEqual(instrument.trading_status, TradingStatus.ACTIVE)

    def test_requires_stable_instrument_identifier_and_enum(self):
        with self.assertRaises(ValueError):
            self.make_instrument(instrument_id=" ")
        with self.assertRaises(TypeError):
            self.make_instrument(asset_class="EQUITY")

    def test_rejects_invalid_tick_and_quantity_metadata(self):
        for field, value in (("tick_size", 0), ("tick_size", float("nan")),
                             ("lot_size", 0), ("minimum_order_quantity", -1)):
            with self.subTest(field=field, value=value), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.make_instrument(**{field: value})

    def test_requires_valid_timezone_and_local_time_pair(self):
        with self.assertRaises(ValueError):
            self.make_instrument(timezone="Not/A_Zone")
        with self.assertRaises(ValueError):
            self.make_instrument(trading_hours=(time(9, 0),))


class SignalTests(unittest.TestCase):
    def make_signal(self, **overrides):
        values = {
            "instrument_id": "XNSE:RELIANCE",
            "symbol": "RELIANCE.NS",
            "timestamp": UTC_NOW,
            "strategy": "ORB + VWAP",
            "side": SignalSide.BUY,
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "reward_risk": 2.0,
            "confidence": 75.0,
            "expected_edge": 1.5,
            "reasons": ("Breakout above opening range",),
            "invalidation_conditions": ("Price closes below VWAP",),
        }
        values.update(overrides)
        return Signal(**values)

    def test_accepts_signal_with_aware_timestamp_and_generated_id(self):
        signal = self.make_signal()
        self.assertIsInstance(signal.signal_id, UUID)
        self.assertEqual(signal.side, SignalSide.BUY)

    def test_rejects_naive_timestamp_and_invalid_confidence(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.make_signal(timestamp=datetime(2026, 10, 5, 9, 30))
        with self.assertRaises(ValueError):
            self.make_signal(confidence=100.1)

    def test_rejects_invalid_prices_and_directional_levels(self):
        with self.assertRaises(ValueError):
            self.make_signal(entry_price=0)
        with self.assertRaises(ValueError):
            self.make_signal(stop_loss=100.0)
        with self.assertRaises(ValueError):
            self.make_signal(side=SignalSide.SELL, stop_loss=99.0)

    def test_hold_signal_cannot_carry_order_levels(self):
        with self.assertRaises(ValueError):
            self.make_signal(side=SignalSide.HOLD)

    def test_rejects_non_tuple_explanations(self):
        with self.assertRaises(ValueError):
            self.make_signal(reasons=["not immutable"])


class OrderTests(unittest.TestCase):
    def make_order(self, **overrides):
        values = {
            "instrument_id": "XNSE:RELIANCE",
            "symbol": "RELIANCE.NS",
            "side": OrderSide.BUY,
            "quantity": 10,
            "order_type": OrderType.MARKET,
            "created_at": UTC_NOW,
        }
        values.update(overrides)
        return Order(**values)

    def test_accepts_market_order_and_generates_order_id(self):
        order = self.make_order()
        self.assertIsInstance(order.order_id, UUID)
        self.assertEqual(order.status, OrderStatus.NEW)

    def test_validates_quantity_prices_and_timestamp(self):
        for quantity in (0, -1, 1.5, True):
            with self.subTest(quantity=quantity), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.make_order(quantity=quantity)
        with self.assertRaises(ValueError):
            self.make_order(created_at=datetime(2026, 10, 5, 9, 30))
        with self.assertRaises(ValueError):
            self.make_order(order_type=OrderType.LIMIT, limit_price=0)

    def test_enforces_order_type_price_requirements(self):
        self.assertEqual(
            self.make_order(order_type=OrderType.LIMIT,
                            limit_price=100).limit_price,
            100,
        )
        self.assertEqual(
            self.make_order(order_type=OrderType.STOP,
                            stop_price=99).stop_price,
            99,
        )
        self.assertEqual(
            self.make_order(
                order_type=OrderType.STOP_LIMIT, stop_price=99, limit_price=98
            ).order_type,
            OrderType.STOP_LIMIT,
        )
        with self.assertRaises(ValueError):
            self.make_order(order_type=OrderType.LIMIT)
        with self.assertRaises(ValueError):
            self.make_order(limit_price=100)

    def test_validates_fill_state(self):
        partial = self.make_order(
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=4,
            average_fill_price=100,
        )
        self.assertEqual(partial.filled_quantity, 4)
        with self.assertRaises(ValueError):
            self.make_order(filled_quantity=1)
        with self.assertRaises(ValueError):
            self.make_order(status=OrderStatus.PARTIALLY_FILLED)
        with self.assertRaises(ValueError):
            self.make_order(
                status=OrderStatus.FILLED,
                filled_quantity=9,
                average_fill_price=100,
            )
        self.assertEqual(
            self.make_order(
                status=OrderStatus.FILLED,
                filled_quantity=10,
                average_fill_price=100,
            ).status,
            OrderStatus.FILLED,
        )


class PositionTests(unittest.TestCase):
    def make_position(self, **overrides):
        values = {
            "instrument_id": "XNSE:RELIANCE",
            "symbol": "RELIANCE.NS",
            "side": PositionSide.LONG,
            "quantity": 10,
            "average_entry_price": 100.0,
            "opened_at": UTC_NOW,
            "stop_loss": 99.0,
            "take_profit": 102.0,
        }
        values.update(overrides)
        return Position(**values)

    def test_accepts_long_and_short_position_levels(self):
        long_position = self.make_position()
        short_position = self.make_position(
            side=PositionSide.SHORT, stop_loss=101.0, take_profit=98.0
        )
        self.assertEqual(long_position.quantity, 10)
        self.assertEqual(short_position.side, PositionSide.SHORT)

    def test_rejects_nonpositive_quantity_price_and_naive_time(self):
        for field, value in (("quantity", 0), ("quantity", -1),
                             ("average_entry_price", 0)):
            with self.subTest(field=field, value=value), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.make_position(**{field: value})
        with self.assertRaises(ValueError):
            self.make_position(opened_at=datetime(2026, 10, 5, 9, 30))

    def test_rejects_stop_or_target_on_wrong_side(self):
        with self.assertRaises(ValueError):
            self.make_position(stop_loss=100)
        with self.assertRaises(ValueError):
            self.make_position(
                side=PositionSide.SHORT, stop_loss=99, take_profit=98
            )


class RiskDecisionTests(unittest.TestCase):
    def test_approved_decision_may_have_no_reason(self):
        decision = RiskDecision(
            status=RiskDecisionStatus.APPROVED, timestamp=UTC_NOW
        )
        self.assertIsInstance(decision.decision_id, UUID)
        self.assertIsNone(decision.reason)

    def test_rejected_decision_requires_nonempty_reason(self):
        with self.assertRaises(ValueError):
            RiskDecision(status=RiskDecisionStatus.REJECTED, timestamp=UTC_NOW)
        with self.assertRaises(ValueError):
            RiskDecision(
                status=RiskDecisionStatus.REJECTED,
                timestamp=UTC_NOW,
                reason="  ",
            )
        rejected = RiskDecision(
            status=RiskDecisionStatus.REJECTED,
            timestamp=UTC_NOW,
            reason="Position limit exceeded",
        )
        self.assertEqual(rejected.reason, "Position limit exceeded")

    def test_rejects_naive_timestamp_and_non_enum_status(self):
        with self.assertRaises(ValueError):
            RiskDecision(
                status=RiskDecisionStatus.APPROVED,
                timestamp=datetime(2026, 10, 5, 9, 30),
            )
        with self.assertRaises(TypeError):
            RiskDecision(status="APPROVED", timestamp=UTC_NOW)


if __name__ == "__main__":
    unittest.main()
