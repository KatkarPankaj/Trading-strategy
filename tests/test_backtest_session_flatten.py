from datetime import date, time, timedelta, timezone
import unittest

import pandas as pd

from stockmarket.core.backtesting.engine import BacktestConfig, BacktestEngine, BacktestSignal
from stockmarket.core.backtesting.execution_model import ExecutionModel
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, OrderSide
from stockmarket.core.sizing import SizingLimits
from stockmarket.core.trading_calendar import TradingCalendar


class SessionFlattenTests(unittest.TestCase):
    def setUp(self):
        self.instrument = default_markets().get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=.01)
        self.calendar = TradingCalendar(
            "America/New_York", time(9, 30), time(16),
            early_closes={date(2026, 11, 27): time(13)})

    def bars(self, day, *, interval="5min"):
        close = self.calendar.close_time_on(date.fromisoformat(day))
        index = pd.date_range(f"{day} 09:30", f"{day} {close}",
                              freq=interval, tz=self.calendar.timezone, inclusive="left")
        return pd.DataFrame({"open": 100., "high": 100.1, "low": 99.9,
                             "close": 100., "volume": 10000.}, index=index)

    def run_backtest(self, bars, side=OrderSide.BUY, *, flatten=True, interval=5,
                     signal=None, participation_rate=None):
        config = BacktestConfig(
            100_000, SizingLimits(.001, 1000), allow_short=True,
            execution=ExecutionModel(fixed_commission=1, slippage_bps=5,
                                     participation_rate=participation_rate),
            session_flatten_before_close=timedelta(minutes=10) if flatten else None,
            bar_interval=timedelta(minutes=interval) if flatten else None)
        return BacktestEngine(config).run(
            {self.instrument.instrument_id: self.instrument},
            {self.instrument.instrument_id: bars},
            signal or (lambda iid, history: BacktestSignal(
                side, 90 if side is OrderSide.BUY else 110, reason="FIXTURE")),
            calendars={self.instrument.instrument_id: self.calendar})

    def test_long_short_close_at_boundary_open_with_costs_no_future_close(self):
        for side in (OrderSide.BUY, OrderSide.SELL):
            with self.subTest(side=side):
                bars = self.bars("2026-10-08")
                boundary = pd.Timestamp("2026-10-08 15:50", tz=self.calendar.timezone)
                bars.loc[boundary, ["open", "high", "low", "close"]] = [101, 160, 95, 150]
                result = self.run_backtest(bars, side)
                self.assertEqual(len(result.trades), 1)
                trade = result.trades[0]
                self.assertEqual(trade.exit_time, boundary)
                self.assertEqual(trade.exit_reason, "END_OF_DAY_FLATTEN")
                expected = 101 * (1 - .0005 if side is OrderSide.BUY else 1 + .0005)
                self.assertAlmostEqual(trade.exit_price, expected)
                self.assertEqual(trade.fees, 2)
                self.assertEqual(trade.net_pnl, trade.gross_pnl - trade.fees)

    def test_early_close_and_dst_use_exchange_local_boundaries(self):
        bars = pd.concat([self.bars("2026-10-30"), self.bars("2026-11-02"),
                          self.bars("2026-11-27")])
        result = self.run_backtest(bars)
        self.assertEqual([str(t.exit_time.astimezone(timezone.utc)) for t in result.trades],
                         ["2026-10-30 19:50:00+00:00", "2026-11-02 20:50:00+00:00",
                          "2026-11-27 17:50:00+00:00"])
        self.assertTrue(all(t.exit_reason == "END_OF_DAY_FLATTEN" for t in result.trades))

    def test_pending_entry_cancelled_and_no_new_signals_after_boundary(self):
        bars = self.bars("2026-10-08")
        calls = []
        def signal(iid, history):
            at = history.index[-1]
            calls.append(at)
            return BacktestSignal(OrderSide.BUY, 90) if at.hour == 15 and at.minute == 45 else None
        result = self.run_backtest(bars, signal=signal)
        self.assertEqual(result.trades, ())
        self.assertEqual(result.fills, ())
        self.assertEqual(calls[-1].strftime("%H:%M"), "15:45")

    def test_missing_boundary_fails_and_default_overnight_behavior_is_preserved(self):
        bars = self.bars("2026-10-08")
        missing = bars.drop(pd.Timestamp("2026-10-08 15:50", tz=self.calendar.timezone))
        with self.assertRaisesRegex(ValueError, "missing scheduled session-flatten bar"):
            self.run_backtest(missing)
        result = self.run_backtest(bars, flatten=False)
        self.assertEqual(result.trades[0].exit_reason, "END_OF_DATA")

    def test_fifteen_minute_alignment_never_uses_future_bar(self):
        result = self.run_backtest(self.bars("2026-10-08", interval="15min"), interval=15)
        self.assertEqual(result.trades[0].exit_time.strftime("%H:%M"), "15:45")

    def test_flatten_respects_participation_and_refuses_unclosed_overnight_position(self):
        bars = self.bars("2026-10-08")
        boundary = pd.Timestamp("2026-10-08 15:50", tz=self.calendar.timezone)
        bars.loc[bars.index >= boundary, "volume"] = 500
        result = self.run_backtest(bars, participation_rate=.01)
        self.assertEqual(result.trades[0].exit_time.strftime("%H:%M"), "15:55")
        self.assertEqual(len(result.fills), 3)
        bars.loc[boundary, ["open", "high", "low", "close"]] = [101, 160, 95, 150]
        result = self.run_backtest(bars, participation_rate=.01)
        # Five shares remain after the first flatten fill: mark at known bar open.
        expected_equity = 100_000 - 10 * 100.05 + 5 * (101 * .9995) - 2 + 5 * 101
        self.assertAlmostEqual(result.equity_curve.loc[boundary], expected_equity)
        bars.loc[bars.index >= boundary, "volume"] = 0
        with self.assertRaisesRegex(ValueError, "insufficient session-flatten liquidity"):
            self.run_backtest(bars, participation_rate=.01)
