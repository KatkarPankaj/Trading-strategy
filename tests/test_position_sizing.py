import unittest

from stockmarket.core import (
    AssetClass,
    BrokerConstraints,
    Instrument,
    OrderSide,
    SizingLimits,
    size_position,
)


def instrument(lot_size=1, minimum_order_quantity=1):
    return Instrument(
        instrument_id="XNSE:A", symbol="A", exchange="NSE", market="IN",
        asset_class=AssetClass.EQUITY, currency="INR", timezone="Asia/Kolkata",
        tick_size=0.05, lot_size=lot_size, minimum_order_quantity=minimum_order_quantity)


def limits(**overrides):
    values = dict(risk_per_trade_pct=0.01, max_order_notional=10_000_000.0)
    values.update(overrides)
    return SizingLimits(**values)


def size(inst=None, side=OrderSide.BUY, **overrides):
    values = dict(entry_price=100.0, stop_price=98.0, equity=1_000_000.0,
                  available_cash=1_000_000.0, limits=limits())
    values.update(overrides)
    return size_position(inst or instrument(), side, **values)


class PositionSizingTests(unittest.TestCase):
    def test_risk_budget_over_stop_distance(self):
        r = size()
        self.assertEqual(r.quantity, 5000)  # 10,000 risk / 2 stop distance
        self.assertEqual(r.risk_budget, 10_000.0)
        self.assertEqual(r.stop_distance, 2.0)
        self.assertEqual(r.binding_constraint, "risk_budget")

    def test_short_side(self):
        self.assertEqual(
            size(side=OrderSide.SELL, stop_price=102.0).quantity, 5000)
        self.assertEqual(size(side=OrderSide.SELL).reason,
                         "STOP_ON_WRONG_SIDE_OF_ENTRY")

    def test_wider_stop_means_smaller_size(self):
        self.assertLess(size(stop_price=90.0).quantity, size().quantity)

    def test_notional_cap(self):
        r = size(limits=limits(max_order_notional=100_000.0))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (1000, "order_notional"))

    def test_cash_cap(self):
        r = size(available_cash=50_000.0,
                 limits=limits(cash_requirement_rate=0.5))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (1000, "account_cash"))

    def test_portfolio_and_sector_caps(self):
        r = size(gross_exposure=950_000.0,
                 limits=limits(max_total_notional_pct=1.0))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (500, "portfolio_exposure"))
        r = size(sector_exposure=290_000.0,
                 limits=limits(max_sector_exposure_pct=0.3))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (100, "sector_exposure"))

    def test_position_cap_accounts_for_existing_position(self):
        r = size(current_position_notional=190_000.0,
                 limits=limits(max_position_notional_pct=0.2))
        self.assertEqual(r.quantity, 100)

    def test_liquidity_cap(self):
        r = size(average_daily_volume=10_000.0,
                 limits=limits(max_participation_rate=0.01))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (100, "liquidity"))
        self.assertEqual(
            size(limits=limits(max_participation_rate=0.01)).reason, "LIQUIDITY_UNKNOWN")

    def test_broker_constraints(self):
        r = size(limits=limits(broker=BrokerConstraints(max_order_quantity=300)))
        self.assertEqual((r.quantity, r.binding_constraint),
                         (300, "broker_quantity"))
        r = size(limits=limits(broker=BrokerConstraints(
            max_order_notional=20_000.0)))
        self.assertEqual(r.quantity, 200)

    def test_lot_size_rounding_floors(self):
        r = size(instrument(lot_size=75), limits=limits(
            max_order_notional=100_000.0))
        self.assertEqual(r.quantity, 975)  # 1000 -> 13 lots
        self.assertEqual(r.quantity % 75, 0)

    def test_below_one_lot_is_rejected(self):
        r = size(instrument(lot_size=100),
                 limits=limits(max_order_notional=5_000.0))
        self.assertEqual((r.quantity, r.reason),
                         (0, "BELOW_MINIMUM_ORDER_SIZE"))
        self.assertFalse(r.approved)

    def test_exhausted_capacity_returns_zero(self):
        r = size(gross_exposure=2_000_000.0,
                 limits=limits(max_total_notional_pct=1.0))
        self.assertEqual((r.quantity, r.reason),
                         (0, "NO_CAPACITY_PORTFOLIO_EXPOSURE"))

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(size(entry_price=float("nan")).reason,
                         "INVALID_ENTRY_PRICE")
        self.assertEqual(size(equity=0.0).reason, "INVALID_EQUITY")
        self.assertEqual(size(available_cash=-1.0).reason,
                         "INVALID_AVAILABLE_CASH")
        self.assertEqual(size(stop_price=100.0).reason,
                         "STOP_ON_WRONG_SIDE_OF_ENTRY")

    def test_invalid_limits_rejected(self):
        with self.assertRaises(ValueError):
            limits(risk_per_trade_pct=0.0)
        with self.assertRaises(ValueError):
            limits(risk_per_trade_pct=1.5)
        with self.assertRaises(ValueError):
            limits(max_participation_rate=2.0)


if __name__ == "__main__":
    unittest.main()
