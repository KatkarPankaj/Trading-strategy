from datetime import datetime
import unittest
from unittest.mock import patch

import pandas as pd

from stockmarket.backtest import _bar_exit, run_backtest, validate_ohlcv_data
from stockmarket.config import TradingConfig
from stockmarket.core import MarketSession
from stockmarket.strategy import add_strategy_columns


def bars(count=6):
    index = pd.date_range(
        "2026-10-05 09:15", periods=count, freq="5min", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {
            "open": [100.0] * count,
            "high": [101.0] * count,
            "low": [99.0] * count,
            "close": [100.0] * count,
            "volume": [100.0] * count,
        },
        index=index,
    )


class BacktestValidationTests(unittest.TestCase):
    def test_rejects_naive_unsorted_duplicate_and_invalid_ohlcv(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            validate_ohlcv_data(bars().set_axis(
                pd.date_range("2026-10-05", periods=6, freq="5min")))

        unsorted = bars().iloc[::-1]
        with self.assertRaisesRegex(ValueError, "sorted"):
            validate_ohlcv_data(unsorted)

        duplicated = bars()
        duplicated.index = duplicated.index.where(
            duplicated.index != duplicated.index[2], duplicated.index[1]
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_ohlcv_data(duplicated)

        invalid = bars()
        invalid.iloc[1, invalid.columns.get_loc("high")] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_ohlcv_data(invalid)

        invalid = bars()
        invalid.iloc[1, invalid.columns.get_loc("high")] = 98.0
        with self.assertRaisesRegex(ValueError, "high must"):
            validate_ohlcv_data(invalid)

        invalid = bars()
        invalid.iloc[1, invalid.columns.get_loc("volume")] = -1.0
        with self.assertRaisesRegex(ValueError, "volume"):
            validate_ohlcv_data(invalid)

    def test_signal_bar_extremes_cannot_exit_before_next_bar_fill(self):
        source = bars()
        source.loc[source.index[2], "high"] = 150.0
        source.loc[source.index[2], "low"] = 50.0
        prepared = source.copy()
        prepared["long_signal"] = [False, False, True, False, False, False]
        prepared["short_signal"] = False
        cfg = TradingConfig(
            starting_capital=1000.0,
            risk_per_trade_pct=0.01,
            stop_loss_pct=0.10,
            take_profit_pct=0.20,
            commission_pct=0.0,
            slippage_pct=0.0,
            max_trades_per_day=1,
        )
        with patch("stockmarket.backtest.add_strategy_columns", return_value=prepared):
            result = run_backtest(source, cfg)

        self.assertEqual(len(result.trades), 1)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["signal_ts"], source.index[2])
        self.assertEqual(trade["entry_ts"], source.index[3])
        self.assertNotEqual(trade["exit_reason"], "stop")
        self.assertNotEqual(trade["exit_reason"], "target")
        self.assertEqual(len(result.equity_curve), len(source))
        self.assertIn("expectancy", result.summary)
        self.assertIn("sharpe_ratio", result.summary)
        self.assertIn("exposure", result.summary)

    def test_cost_multipliers_increase_costs_and_reduce_net_pnl(self):
        source = bars()
        prepared = source.copy()
        prepared["long_signal"] = [False, False, True, False, False, False]
        prepared["short_signal"] = False
        cfg = TradingConfig(
            starting_capital=10000.0,
            risk_per_trade_pct=0.01,
            stop_loss_pct=0.02,
            take_profit_pct=0.03,
            commission_pct=0.001,
            slippage_pct=0.001,
            max_trades_per_day=1,
        )
        with patch("stockmarket.backtest.add_strategy_columns", return_value=prepared):
            baseline = run_backtest(source, cfg)
            stressed = run_backtest(
                source,
                cfg,
                commission_multiplier=2.0,
                slippage_multiplier=2.0,
            )
        self.assertLessEqual(
            stressed.summary["net_pnl"], baseline.summary["net_pnl"])
        self.assertGreaterEqual(
            stressed.trades.iloc[0]["commission"],
            baseline.trades.iloc[0]["commission"],
        )

    def test_legacy_summary_keeps_numeric_win_rate_and_profit_factor(self):
        source = bars()
        result = run_backtest(source, TradingConfig())
        self.assertEqual(result.summary["total_trades"], 0)
        self.assertEqual(result.summary["win_rate"], 0.0)
        self.assertEqual(result.summary["profit_factor"], 0.0)

    def test_typical_vwap_remains_default_and_close_is_experimental(self):
        source = bars()
        source["high"] = [102, 105, 103, 104, 102, 101]
        source["low"] = [98, 99, 97, 98, 99, 100]
        typical = add_strategy_columns(source, TradingConfig())
        explicit = add_strategy_columns(
            source, TradingConfig(vwap_price_source="typical")
        )
        close = add_strategy_columns(
            source, TradingConfig(vwap_price_source="close")
        )
        pd.testing.assert_series_equal(typical["vwap"], explicit["vwap"])
        self.assertFalse(typical["vwap"].equals(close["vwap"]))
        with self.assertRaisesRegex(ValueError, "vwap_price_source"):
            add_strategy_columns(
                source, TradingConfig(vwap_price_source="hlc3"))

    def test_gap_through_stop_fills_at_open_and_stop_wins_ambiguous_bar(self):
        position = {
            "side": "long",
            "stop_price": 90.0,
            "target_price": 120.0,
            "time_exit_ts": pd.Timestamp("2026-10-05 15:00", tz="Asia/Kolkata"),
        }
        row = pd.Series(
            {"open": 85.0, "high": 125.0, "low": 80.0, "close": 100.0}
        )
        timestamp = pd.Timestamp("2026-10-05 10:00", tz="Asia/Kolkata")
        reason, fill_price = _bar_exit(
            position,
            timestamp,
            row,
            MarketSession.from_config(TradingConfig()),
        )
        self.assertEqual(reason, "stop")
        self.assertEqual(fill_price, 85.0)

    def test_utc_input_is_normalized_to_exchange_timezone_before_session_processing(self):
        source = bars()
        utc_source = source.copy()
        utc_source.index = utc_source.index.tz_convert("UTC")
        prepared = source.copy()
        prepared["long_signal"] = [False, False, True, False, False, False]
        prepared["short_signal"] = False
        cfg = TradingConfig(
            starting_capital=1000.0,
            risk_per_trade_pct=0.01,
            stop_loss_pct=0.1,
            take_profit_pct=0.2,
            commission_pct=0.0,
            slippage_pct=0.0,
        )
        with patch("stockmarket.backtest.add_strategy_columns", return_value=prepared):
            result = run_backtest(utc_source, cfg)
        self.assertEqual(len(result.equity_curve), len(source))
        self.assertEqual(
            result.equity_curve["timestamp"].dt.tz.zone, "Asia/Kolkata")

    def test_evaluation_date_bounds_exclude_indicator_warmup_from_simulation(self):
        first_day = bars()
        second_day = first_day.copy()
        second_day.index = second_day.index + pd.Timedelta(days=1)
        combined = pd.concat([first_day, second_day]).sort_index()
        prepared = combined.copy()
        prepared["long_signal"] = [False] * len(first_day) + [
            False, False, True, False, False, False
        ]
        prepared["short_signal"] = False
        evaluation_date = second_day.index[0].date()
        with patch(
            "stockmarket.backtest.add_strategy_columns",
            return_value=prepared,
        ):
            result = run_backtest(
                combined,
                TradingConfig(
                    starting_capital=1000,
                    risk_per_trade_pct=0.01,
                    stop_loss_pct=0.1,
                    take_profit_pct=0.2,
                    commission_pct=0,
                    slippage_pct=0,
                ),
                evaluation_start=evaluation_date,
                evaluation_end=evaluation_date,
            )
        self.assertTrue(
            (result.equity_curve["timestamp"].dt.date == evaluation_date).all()
        )
        self.assertTrue(
            (result.trades["signal_ts"].dt.date == evaluation_date).all()
        )


if __name__ == "__main__":
    unittest.main()
