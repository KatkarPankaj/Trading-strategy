from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import unittest

from stockmarket.core import (
    AssetClass,
    Instrument,
    Order,
    OrderIntent,
    OrderSide,
    OrderType,
    PortfolioRiskLimits,
    PortfolioRiskState,
    PositionSide,
    RiskContext,
    RiskDecisionStatus,
    RiskEngine,
    RiskLimits,
    order_key,
)

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
DATA_AT = NOW - timedelta(seconds=30)


def portfolio_limits(**overrides):
    values = dict(
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
    )
    values.update(overrides)
    return PortfolioRiskLimits(**values)


class PortfolioRiskTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument(
            instrument_id="XNSE:RELIANCE", symbol="RELIANCE", exchange="NSE",
            market="IN", asset_class=AssetClass.EQUITY, currency="INR",
            timezone="Asia/Kolkata", tick_size=0.05, shortable=True)
        self.limits = RiskLimits(
            max_position_quantity=10000, max_order_notional=500000.0,
            max_open_positions=5, max_trades_per_day=10,
            cash_requirement_rate=0.2, entry_window=(time(9, 30), time(13, 30)),
            max_market_data_age=timedelta(minutes=2))
        self.engine = RiskEngine(self.limits, portfolio_limits())

    def state(self, **overrides):
        values = dict(
            equity=1_000_000.0, peak_equity=1_000_000.0, daily_pnl=0.0,
            gross_notional_exposure=100_000.0, sector="ENERGY",
            sector_exposure={"ENERGY": 50_000.0}, correlated_exposure=50_000.0,
            current_position_notional=0.0, orders_last_minute=0,
            recent_order_keys=frozenset(), average_daily_traded_value=5e8,
            expected_slippage_bps=5.0)
        values.update(overrides)
        return PortfolioRiskState(**values)

    def order(self, **overrides):
        values = dict(
            instrument_id=self.instrument.instrument_id,
            symbol=self.instrument.symbol, side=OrderSide.BUY, quantity=100,
            order_type=OrderType.MARKET, created_at=NOW)
        values.update(overrides)
        return Order(**values)

    def context(self, portfolio="default", **overrides):
        values = dict(
            assessed_at=NOW, market_data_timestamp=DATA_AT, market_data_valid=True,
            reference_price=100.0, available_cash=900_000.0, estimated_fees=20.0,
            current_position_quantity=0, current_position_side=None,
            open_positions=0, trades_today=0, stop_loss=99.0, take_profit=102.0,
            portfolio=self.state() if portfolio == "default" else portfolio)
        values.update(overrides)
        return RiskContext(**values)

    def evaluate(self, order=None, **ctx):
        return self.engine.evaluate(order or self.order(), self.instrument, self.context(**ctx))

    def assert_rejected(self, decision, code):
        self.assertEqual(decision.status, RiskDecisionStatus.REJECTED)
        self.assertTrue(decision.reason.startswith(
            code + ":"), decision.reason)

    def test_valid_order_approved(self):
        self.assertEqual(self.evaluate().status, RiskDecisionStatus.APPROVED)

    def test_missing_portfolio_state_rejected(self):
        self.assert_rejected(self.evaluate(portfolio=None),
                             "PORTFOLIO_STATE_MISSING")

    def test_risk_per_trade(self):
        # 100 shares * 1 stop distance = 100 risk; widen stop to breach 1% of 1M.
        self.assert_rejected(
            self.evaluate(self.order(quantity=500),
                          stop_loss=75.0, take_profit=200.0),
            "MAX_RISK_PER_TRADE_EXCEEDED")

    def test_daily_loss(self):
        self.assert_rejected(self.evaluate(portfolio=self.state(daily_pnl=-25_000.0)),
                             "MAX_DAILY_LOSS_BREACHED")

    def test_drawdown(self):
        self.assert_rejected(self.evaluate(portfolio=self.state(peak_equity=1_200_000.0)),
                             "MAX_DRAWDOWN_BREACHED")

    def test_position_size(self):
        self.assert_rejected(self.evaluate(self.order(quantity=2100)),
                             "MAX_POSITION_SIZE_EXCEEDED")

    def test_total_exposure_and_leverage(self):
        s = self.state(gross_notional_exposure=995_000.0)
        self.assert_rejected(self.evaluate(portfolio=s),
                             "MAX_NOTIONAL_EXPOSURE_EXCEEDED")
        engine = RiskEngine(self.limits, portfolio_limits(
            max_total_notional_pct=5.0))
        s2 = self.state(gross_notional_exposure=1_995_000.0)
        decision = engine.evaluate(
            self.order(), self.instrument, self.context(portfolio=s2))
        self.assert_rejected(decision, "MAX_LEVERAGE_EXCEEDED")

    def test_sector_exposure(self):
        s = self.state(sector_exposure={"ENERGY": 295_000.0})
        self.assert_rejected(self.evaluate(portfolio=s),
                             "MAX_SECTOR_EXPOSURE_EXCEEDED")
        self.assert_rejected(self.evaluate(
            portfolio=self.state(sector=None)), "SECTOR_UNKNOWN")

    def test_correlated_exposure(self):
        self.assert_rejected(
            self.evaluate(portfolio=self.state(correlated_exposure=395_000.0)),
            "MAX_CORRELATED_EXPOSURE_EXCEEDED")
        self.assert_rejected(
            self.evaluate(portfolio=self.state(correlated_exposure=None)),
            "CORRELATED_EXPOSURE_UNKNOWN")

    def test_order_rate(self):
        self.assert_rejected(self.evaluate(portfolio=self.state(orders_last_minute=5)),
                             "MAX_ORDERS_PER_MINUTE_EXCEEDED")
        self.assert_rejected(self.evaluate(portfolio=self.state(orders_last_minute=None)),
                             "ORDER_RATE_UNKNOWN")

    def test_liquidity_and_slippage(self):
        self.assert_rejected(
            self.evaluate(portfolio=self.state(
                average_daily_traded_value=10.0)),
            "MIN_LIQUIDITY_NOT_MET")
        self.assert_rejected(
            self.evaluate(portfolio=self.state(expected_slippage_bps=50.0)),
            "MAX_SLIPPAGE_EXCEEDED")
        self.assert_rejected(
            self.evaluate(portfolio=self.state(expected_slippage_bps=None)),
            "SLIPPAGE_UNKNOWN")

    def test_duplicate_order(self):
        order = self.order()
        s = self.state(recent_order_keys=frozenset({order_key(order)}))
        self.assert_rejected(self.evaluate(
            order, portfolio=s), "DUPLICATE_ORDER")

    def test_invalid_state_rejected(self):
        self.assert_rejected(self.evaluate(portfolio=self.state(equity=0.0)),
                             "PORTFOLIO_STATE_INVALID")
        self.assert_rejected(self.evaluate(portfolio=self.state(daily_pnl=float("nan"))),
                             "PORTFOLIO_STATE_INVALID")

    def test_exits_bypass_entry_limits(self):
        order = self.order(side=OrderSide.SELL, quantity=10)
        ctx = self.context(portfolio=self.state(daily_pnl=-90_000.0, peak_equity=2e6),
                           intent=OrderIntent.EXIT, current_position_quantity=10,
                           current_position_side=PositionSide.LONG, open_positions=1)
        self.assertEqual(self.engine.evaluate(order, self.instrument, ctx).status,
                         RiskDecisionStatus.APPROVED)


class PortfolioLimitConfigTests(unittest.TestCase):
    def test_unconfigured_control_cannot_be_silently_omitted(self):
        with self.assertRaises(ValueError):
            portfolio_limits(max_leverage=None)

    def test_explicit_disable_requires_reason(self):
        with self.assertRaises(ValueError):
            portfolio_limits(max_leverage=None, disabled={"max_leverage": " "})
        limits = portfolio_limits(
            max_leverage=None, disabled={"max_leverage": "cash-only account"})
        self.assertEqual(RiskEngine(None, limits).disabled_controls,
                         {"max_leverage": "cash-only account"})

    def test_configured_and_disabled_conflict(self):
        with self.assertRaises(ValueError):
            portfolio_limits(disabled={"max_leverage": "reason"})

    def test_invalid_values_and_unknown_controls(self):
        for bad in ({"max_drawdown_pct": 0.0}, {"max_drawdown_pct": 1.5},
                    {"max_leverage": float("inf")}, {
                "max_orders_per_minute": 0},
                {"duplicate_order_prevention": False}):
            with self.assertRaises(ValueError):
                portfolio_limits(**bad)
        with self.assertRaises(ValueError):
            portfolio_limits(disabled={"nonsense": "x"})

    def test_disabled_control_is_skipped(self):
        limits = portfolio_limits(
            max_slippage_bps=None, disabled={"max_slippage_bps": "no estimate available"})
        inst = Instrument(
            instrument_id="X:A", symbol="A", exchange="X", market="M",
            asset_class=AssetClass.EQUITY, currency="INR", timezone="Asia/Kolkata",
            tick_size=0.05)
        base = RiskLimits(
            max_position_quantity=1000, max_order_notional=500000.0,
            max_open_positions=5, max_trades_per_day=10, cash_requirement_rate=0.2,
            entry_window=(time(9, 30), time(13, 30)))
        state = PortfolioRiskState(
            equity=1e6, peak_equity=1e6, daily_pnl=0.0, gross_notional_exposure=0.0,
            sector="S", correlated_exposure=0.0, orders_last_minute=0,
            average_daily_traded_value=1e9)
        ctx = RiskContext(
            assessed_at=NOW, market_data_timestamp=DATA_AT, market_data_valid=True,
            reference_price=100.0, available_cash=9e5, estimated_fees=1.0,
            current_position_quantity=0, current_position_side=None, open_positions=0,
            trades_today=0, stop_loss=99.0, take_profit=102.0, portfolio=state)
        order = Order(instrument_id="X:A", symbol="A", side=OrderSide.BUY, quantity=10,
                      order_type=OrderType.MARKET, created_at=NOW)
        decision = RiskEngine(base, limits).evaluate(order, inst, ctx)
        self.assertEqual(
            decision.status, RiskDecisionStatus.APPROVED, decision.reason)


if __name__ == "__main__":
    unittest.main()
