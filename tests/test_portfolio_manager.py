from datetime import datetime, timedelta, timezone
import unittest

from stockmarket.core import (
    AssetClass,
    Instrument,
    OrderSide,
    PortfolioError,
    PortfolioManager,
    PositionSide,
)

T0 = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)


def inst(symbol="RELIANCE", currency="INR"):
    return Instrument(
        instrument_id=f"X:{symbol}", symbol=symbol, exchange="X", market="M",
        asset_class=AssetClass.EQUITY, currency=currency, timezone="UTC", tick_size=0.01)


class PortfolioManagerTests(unittest.TestCase):
    def setUp(self):
        self.pm = PortfolioManager("INR", 1_000_000.0)
        self.a = inst()

    def test_buy_updates_cash_position_and_fees(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 100, 100.0, T0, fee=20.0,
                           slippage=5.0, sector="ENERGY")
        self.assertEqual(self.pm.cash["INR"], 1_000_000 - 10_000 - 20)
        pos = self.pm.positions()["X:RELIANCE"]
        self.assertEqual((pos.side, pos.quantity, pos.average_entry_price),
                         (PositionSide.LONG, 100, 100.0))
        self.assertEqual((self.pm.fees, self.pm.slippage), (20.0, 5.0))
        self.assertAlmostEqual(self.pm.equity, 1_000_000 - 20)

    def test_average_entry_and_partial_close_realizes_pnl(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 100, 100.0, T0)
        self.pm.apply_fill(self.a, OrderSide.BUY, 100, 110.0, T0)
        self.assertEqual(self.pm.positions()[
                         "X:RELIANCE"].average_entry_price, 105.0)
        self.pm.apply_fill(self.a, OrderSide.SELL, 50, 115.0, T0)
        self.assertEqual(self.pm.realized_pnl, 500.0)
        self.assertEqual(self.pm.positions()["X:RELIANCE"].quantity, 150)

    def test_mark_to_market_unrealized(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 100, 100.0, T0)
        self.pm.mark("X:RELIANCE", 103.0, T0)
        self.assertEqual(self.pm.unrealized_pnl, 300.0)
        self.assertEqual(self.pm.equity, 1_000_300.0)

    def test_short_and_flip(self):
        self.pm.apply_fill(self.a, OrderSide.SELL, 100, 100.0, T0)
        self.assertEqual(self.pm.positions()[
                         "X:RELIANCE"].side, PositionSide.SHORT)
        self.pm.mark("X:RELIANCE", 90.0, T0)
        self.assertEqual(self.pm.unrealized_pnl, 1000.0)
        self.pm.apply_fill(self.a, OrderSide.BUY, 150, 90.0, T0)
        self.assertEqual(self.pm.realized_pnl, 1000.0)
        pos = self.pm.positions()["X:RELIANCE"]
        self.assertEqual((pos.side, pos.quantity, pos.average_entry_price),
                         (PositionSide.LONG, 50, 90.0))

    def test_full_close_removes_position(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 10, 100.0, T0)
        self.pm.apply_fill(self.a, OrderSide.SELL, 10, 90.0, T0)
        self.assertEqual(self.pm.positions(), {})
        self.assertEqual(self.pm.realized_pnl, -100.0)

    def test_insufficient_cash_rejected_without_state_change(self):
        with self.assertRaises(PortfolioError):
            self.pm.apply_fill(self.a, OrderSide.BUY, 100_000, 100.0, T0)
        self.assertEqual(self.pm.positions(), {})
        self.assertEqual(self.pm.cash["INR"], 1_000_000.0)

    def test_invalid_fills_rejected(self):
        for kwargs in (dict(quantity=0, price=1.0), dict(quantity=1, price=-1.0),
                       dict(quantity=1, price=float("nan")), dict(quantity=1.5, price=1.0)):
            with self.assertRaises(PortfolioError):
                self.pm.apply_fill(self.a, OrderSide.BUY,
                                   timestamp=T0, **kwargs)
        with self.assertRaises(PortfolioError):
            self.pm.apply_fill(self.a, OrderSide.BUY, 1,
                               1.0, datetime(2026, 1, 1))
        with self.assertRaises(PortfolioError):
            self.pm.apply_fill(self.a, OrderSide.BUY, 1, 1.0, T0, fee=-1.0)

    def test_multi_currency_requires_fx_and_converts(self):
        usd = inst("AAPL", "USD")
        with self.assertRaises(PortfolioError):
            self.pm.deposit("USD", 10_000.0)
        self.pm.set_fx_rate("USD", 80.0)
        self.pm.deposit("USD", 10_000.0)
        self.assertEqual(self.pm.equity, 1_000_000 + 800_000)
        self.pm.apply_fill(usd, OrderSide.BUY, 10, 100.0, T0, sector="TECH")
        self.assertEqual(self.pm.cash["USD"], 9_000.0)
        self.assertEqual(self.pm.currency_exposure()[
                         "USD"], 9_000 * 80 + 1_000 * 80)
        self.pm.set_fx_rate("USD", 90.0)
        self.assertEqual(self.pm.gross_exposure, 90_000.0)

    def test_realized_pnl_uses_fx_at_fill_time(self):
        usd = inst("AAPL", "USD")
        self.pm.set_fx_rate("USD", 80.0)
        self.pm.deposit("USD", 10_000.0)
        self.pm.apply_fill(usd, OrderSide.BUY, 10, 100.0, T0)
        self.pm.apply_fill(usd, OrderSide.SELL, 10, 110.0, T0)
        self.assertEqual(self.pm.realized_pnl, 100 * 80.0)

    def test_convert_cash(self):
        self.pm.set_fx_rate("USD", 80.0)
        self.pm.convert_cash("INR", "USD", 80_000.0)
        self.assertEqual(self.pm.cash["USD"], 1000.0)
        with self.assertRaises(PortfolioError):
            self.pm.convert_cash("INR", "USD", 10_000_000.0)

    def test_sector_exposure(self):
        b = inst("TCS")
        self.pm.apply_fill(self.a, OrderSide.BUY, 100,
                           100.0, T0, sector="ENERGY")
        self.pm.apply_fill(b, OrderSide.BUY, 10, 200.0, T0, sector="IT")
        self.assertEqual(self.pm.sector_exposure(), {
                         "ENERGY": 10_000.0, "IT": 2_000.0})

    def test_drawdown_and_peak(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 1000, 100.0, T0)
        self.pm.mark("X:RELIANCE", 110.0, T0)  # equity 1,010,000 = peak
        self.pm.mark("X:RELIANCE", 99.0, T0)
        self.assertEqual(self.pm.peak_equity, 1_010_000.0)
        self.assertAlmostEqual(self.pm.drawdown, 11_000 / 1_010_000)

    def test_daily_and_monthly_pnl_reset_on_new_period(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 1000, 100.0, T0)
        self.pm.mark("X:RELIANCE", 101.0, T0)
        self.assertEqual(self.pm.daily_pnl, 1000.0)
        nxt = T0 + timedelta(days=1)
        self.pm.mark("X:RELIANCE", 103.0, nxt)
        # baseline = prior day's close
        self.assertEqual(self.pm.daily_pnl, 2000.0)
        self.assertEqual(self.pm.monthly_pnl, 3000.0)
        month = T0 + timedelta(days=30)
        self.pm.mark("X:RELIANCE", 104.0, month)
        self.assertEqual(self.pm.monthly_pnl, 1000.0)

    def test_risk_state_bridges_to_risk_engine(self):
        self.pm.apply_fill(self.a, OrderSide.BUY, 100,
                           100.0, T0, sector="ENERGY")
        state = self.pm.risk_state(
            "X:RELIANCE", sector="ENERGY", orders_last_minute=0)
        self.assertEqual(state.equity, self.pm.equity)
        self.assertEqual(state.gross_notional_exposure, 10_000.0)
        self.assertEqual(state.current_position_notional, 10_000.0)
        self.assertEqual(state.sector_exposure["ENERGY"], 10_000.0)


if __name__ == "__main__":
    unittest.main()
