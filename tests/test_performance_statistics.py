from datetime import datetime, timezone
import unittest

import pandas as pd

from stockmarket.validation.statistics import calculate_performance_metrics


def equity_curve(values, position_open=None):
    timestamps = pd.date_range(
        "2026-01-01 12:00", periods=len(values), freq="D", tz="UTC"
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "equity": values,
            "position_open": position_open or [False] * len(values),
        }
    )


class PerformanceStatisticsTests(unittest.TestCase):
    def test_trade_metrics_and_consecutive_streaks(self):
        trades = pd.DataFrame({"net_pnl": [10.0, 5.0, -4.0, -6.0, 0.0, 3.0]})
        metrics = calculate_performance_metrics(
            trades,
            equity_curve([1000, 1010, 1015, 1011, 1005, 1005, 1008]),
            starting_capital=1000.0,
        )
        self.assertEqual(metrics["total_trades"], 6)
        self.assertAlmostEqual(metrics["win_rate"], 0.5)
        self.assertAlmostEqual(metrics["expectancy"], 8.0 / 6.0)
        self.assertAlmostEqual(metrics["profit_factor"], 18.0 / 10.0)
        self.assertAlmostEqual(metrics["average_win"], 6.0)
        self.assertAlmostEqual(metrics["average_loss"], -5.0)
        self.assertEqual(metrics["max_consecutive_wins"], 2)
        self.assertEqual(metrics["max_consecutive_losses"], 2)
        self.assertAlmostEqual(metrics["exposure"], 0.0)

    def test_no_trades_and_no_losses_are_json_safe_undefined_metrics(self):
        empty = calculate_performance_metrics(
            pd.DataFrame(columns=["net_pnl"]),
            equity_curve([1000, 1000, 1000]),
            starting_capital=1000,
        )
        self.assertIsNone(empty["expectancy"])
        self.assertIsNone(empty["win_rate"])
        self.assertIsNone(empty["sharpe_ratio"])

        no_losses = calculate_performance_metrics(
            pd.DataFrame({"net_pnl": [10.0, 5.0]}),
            equity_curve([1000, 1010, 1015]),
            starting_capital=1000,
        )
        self.assertIsNone(no_losses["profit_factor"])
        self.assertEqual(
            no_losses["profit_factor_status"], "undefined_no_losses")

    def test_drawdown_and_exposure_are_based_on_observed_equity(self):
        metrics = calculate_performance_metrics(
            pd.DataFrame({"net_pnl": [-10.0]}),
            equity_curve([100.0, 110.0, 88.0, 95.0],
                         [False, True, True, False]),
            starting_capital=100.0,
        )
        self.assertAlmostEqual(metrics["maximum_drawdown"], -0.2)
        self.assertAlmostEqual(metrics["exposure"], 0.5)
        self.assertEqual(metrics["sharpe_status"], "defined")

    def test_short_trade_metrics_use_supplied_net_pnl(self):
        trades = pd.DataFrame({"net_pnl": [12.0, -3.0]})
        metrics = calculate_performance_metrics(
            trades,
            equity_curve([100.0, 112.0, 109.0]),
            starting_capital=100.0,
        )
        self.assertAlmostEqual(metrics["expectancy"], 4.5)
        self.assertAlmostEqual(metrics["total_return"], 0.09)

    def test_rejects_invalid_equity_curve(self):
        naive = pd.DataFrame(
            {"timestamp": [datetime(2026, 1, 1)], "equity": [
                100], "position_open": [False]}
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            calculate_performance_metrics(
                pd.DataFrame(columns=["net_pnl"]), naive, starting_capital=100
            )


if __name__ == "__main__":
    unittest.main()
