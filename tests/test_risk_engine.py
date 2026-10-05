from datetime import datetime, time, timedelta, timezone
from dataclasses import replace
import unittest

from stockmarket.core import (
    AssetClass,
    Instrument,
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
    TradingStatus,
)


NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
MARKET_DATA_AT = NOW - timedelta(seconds=30)


class RiskEngineTests(unittest.TestCase):
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
        self.limits = RiskLimits(
            max_position_quantity=200,
            max_order_notional=50000.0,
            max_open_positions=3,
            max_trades_per_day=10,
            cash_requirement_rate=0.20,
            entry_window=(time(9, 30), time(13, 30)),
            minimum_reward_risk=1.1,
            minimum_expected_edge=40.0,
            max_market_data_age=timedelta(minutes=2),
        )
        self.engine = RiskEngine(self.limits)
        self.signal = Signal(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol,
            timestamp=MARKET_DATA_AT,
            strategy="ORB + VWAP",
            side=SignalSide.BUY,
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            reward_risk=2.0,
            expected_edge=100.0,
        )

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

    def make_context(self, **overrides):
        values = {
            "assessed_at": NOW,
            "market_data_timestamp": MARKET_DATA_AT,
            "market_data_valid": True,
            "reference_price": 100.0,
            "available_cash": 10000.0,
            "estimated_fees": 20.0,
            "current_position_quantity": 0,
            "current_position_side": None,
            "open_positions": 0,
            "trades_today": 0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "signal": self.signal,
        }
        values.update(overrides)
        return RiskContext(**values)

    def evaluate(self, *, order=None, context=None, instrument=None):
        return self.engine.evaluate(
            order or self.make_order(),
            instrument or self.instrument,
            context or self.make_context(),
        )

    def assert_rejected(self, decision, reason_code):
        self.assertEqual(decision.status, RiskDecisionStatus.REJECTED)
        self.assertTrue(decision.reason.startswith(reason_code + ":"))
        self.assertGreater(len(decision.reason), len(reason_code) + 1)

    def test_approves_valid_order(self):
        decision = self.evaluate()
        self.assertEqual(decision.status, RiskDecisionStatus.APPROVED)
        self.assertIn("all applicable", decision.reason)

    def test_rejects_insufficient_cash_for_configured_margin_and_fees(self):
        decision = self.evaluate(
            context=self.make_context(available_cash=100.0))
        self.assert_rejected(decision, "INSUFFICIENT_CASH_OR_MARGIN")

    def test_rejects_max_position_quantity_and_order_notional(self):
        qty_decision = self.evaluate(order=self.make_order(quantity=201))
        self.assert_rejected(qty_decision, "MAX_POSITION_QUANTITY_EXCEEDED")

        notional_signal = replace(
            self.signal,
            entry_price=501.0,
            stop_loss=490.0,
            take_profit=525.0,
        )
        notional_context = self.make_context(
            reference_price=501.0,
            stop_loss=490.0,
            take_profit=525.0,
            signal=notional_signal,
        )
        notional_decision = self.evaluate(
            order=self.make_order(
                quantity=100, signal_id=notional_signal.signal_id
            ),
            context=notional_context,
        )
        self.assert_rejected(notional_decision, "MAX_ORDER_NOTIONAL_EXCEEDED")

    def test_raw_invalid_quantity_and_limit_price_return_rejections(self):
        base = {
            "instrument": self.instrument,
            "side": OrderSide.BUY,
            "order_type": OrderType.MARKET,
            "created_at": NOW,
            "context": self.make_context(),
        }
        quantity_decision = self.engine.evaluate_proposal(
            quantity=0, **base
        )
        self.assert_rejected(quantity_decision, "INVALID_ORDER")

        price_decision = self.engine.evaluate_proposal(
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=0,
            **{key: value for key, value in base.items() if key != "order_type"},
        )
        self.assert_rejected(price_decision, "INVALID_ORDER")

    def test_invalid_reference_price_and_missing_market_data_fail_closed(self):
        invalid_price = self.evaluate(
            context=self.make_context(reference_price=0.0)
        )
        self.assert_rejected(invalid_price, "REFERENCE_PRICE_INVALID")

        missing_data = self.evaluate(
            context=self.make_context(
                market_data_valid=None,
                market_data_timestamp=None,
                reference_price=None,
            )
        )
        self.assert_rejected(missing_data, "MARKET_DATA_INVALID")

    def test_rejects_stale_or_future_market_data(self):
        stale = self.evaluate(
            context=self.make_context(
                market_data_timestamp=NOW - timedelta(minutes=3)
            )
        )
        self.assert_rejected(stale, "MARKET_DATA_STALE")

        future = self.evaluate(
            context=self.make_context(
                market_data_timestamp=NOW + timedelta(seconds=1)
            )
        )
        self.assert_rejected(future, "MARKET_DATA_FROM_FUTURE")

    def test_rejects_open_position_and_daily_trade_limits(self):
        positions = self.evaluate(
            context=self.make_context(open_positions=3)
        )
        self.assert_rejected(positions, "MAX_OPEN_POSITIONS_EXCEEDED")

        trades = self.evaluate(
            context=self.make_context(trades_today=10)
        )
        self.assert_rejected(trades, "MAX_TRADES_PER_DAY_EXCEEDED")

    def test_rejects_outside_entry_window_and_unconfigured_window(self):
        outside = self.evaluate(
            context=self.make_context(
                assessed_at=datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc),
                market_data_timestamp=datetime(
                    2026, 10, 5, 9, 59, 30, tzinfo=timezone.utc
                ),
                signal=replace(
                    self.signal,
                    timestamp=datetime(
                        2026, 10, 5, 9, 59, 30, tzinfo=timezone.utc
                    ),
                ),
            )
        )
        self.assert_rejected(outside, "OUTSIDE_ENTRY_WINDOW")

        no_window = RiskEngine(
            RiskLimits(
                max_position_quantity=200,
                max_order_notional=50000.0,
                max_open_positions=3,
                max_trades_per_day=10,
                cash_requirement_rate=1.0,
                entry_window=None,
            )
        ).evaluate(self.make_order(), self.instrument, self.make_context())
        self.assert_rejected(no_window, "ENTRY_WINDOW_UNCONFIGURED")

    def test_rejects_missing_and_invalid_protective_levels_or_quality(self):
        missing_stop = self.evaluate(
            context=self.make_context(
                stop_loss=None,
                signal=replace(self.signal, stop_loss=None),
            )
        )
        self.assert_rejected(missing_stop, "STOP_LOSS_MISSING")

        low_reward_risk = Signal(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol,
            timestamp=MARKET_DATA_AT,
            strategy="ORB + VWAP",
            side=SignalSide.BUY,
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=100.5,
            expected_edge=100.0,
        )
        rr_context = self.make_context(
            signal=low_reward_risk,
            stop_loss=99.0,
            take_profit=100.5,
        )
        rr_decision = self.evaluate(
            order=self.make_order(signal_id=low_reward_risk.signal_id),
            context=rr_context,
        )
        self.assert_rejected(rr_decision, "MINIMUM_REWARD_RISK_NOT_MET")

        low_edge = Signal(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol,
            timestamp=MARKET_DATA_AT,
            strategy="ORB + VWAP",
            side=SignalSide.BUY,
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            expected_edge=1.0,
        )
        edge_decision = self.evaluate(
            order=self.make_order(signal_id=low_edge.signal_id),
            context=self.make_context(signal=low_edge),
        )
        self.assert_rejected(edge_decision, "MINIMUM_EXPECTED_EDGE_NOT_MET")

    def test_rejects_contradictory_position_snapshot(self):
        decision = self.evaluate(
            context=self.make_context(
                current_position_quantity=10,
                current_position_side=PositionSide.LONG,
                open_positions=0,
            )
        )
        self.assert_rejected(decision, "POSITION_STATE_CONTRADICTORY")

    def test_exit_is_checked_against_existing_position_not_entry_caps(self):
        order = self.make_order(side=OrderSide.SELL,
                                quantity=10, signal_id=None)
        context = self.make_context(
            intent=OrderIntent.EXIT,
            current_position_quantity=10,
            current_position_side=PositionSide.LONG,
            open_positions=1,
            trades_today=None,
            available_cash=None,
            estimated_fees=20.0,
            signal=None,
            stop_loss=None,
            take_profit=None,
        )
        decision = self.evaluate(order=order, context=context)
        self.assertEqual(decision.status, RiskDecisionStatus.APPROVED)

        too_many = self.evaluate(
            order=self.make_order(side=OrderSide.SELL,
                                  quantity=11, signal_id=None),
            context=context,
        )
        self.assert_rejected(too_many, "EXIT_QUANTITY_EXCEEDS_POSITION")

    def test_missing_order_or_context_is_rejected_with_reason(self):
        missing_order = self.engine.evaluate(
            None, self.instrument, self.make_context())
        self.assert_rejected(missing_order, "ORDER_MISSING")

        missing_context = self.engine.evaluate(
            self.make_order(), self.instrument, None)
        self.assert_rejected(missing_context, "RISK_CONTEXT_MISSING")


if __name__ == "__main__":
    unittest.main()
