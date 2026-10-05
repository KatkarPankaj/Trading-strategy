from datetime import date, datetime, time, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from stockmarket.config import TradingConfig
from stockmarket.validation.walk_forward import (
    WalkForwardConfig,
    walk_forward_validate,
)


def synthetic_sessions(count=12):
    timestamps = []
    rows = []
    current_date = date(2026, 1, 5)
    session_number = 0
    while session_number < count:
        if current_date.weekday() < 5:
            for bar in range(6):
                timestamps.append(
                    datetime.combine(current_date, time(9, 15)).replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")
                    )
                    + timedelta(minutes=5 * bar)
                )
                if bar < 3:
                    rows.append((100.0, 100.5, 99.5, 100.0, 100.0))
                elif bar == 3:
                    rows.append((100.5, 102.0, 100.5, 101.0, 500.0))
                elif bar == 4:
                    rows.append((101.0, 101.1, 100.5, 100.8, 100.0))
                else:
                    rows.append((100.8, 101.0, 100.7, 100.9, 100.0))
            session_number += 1
        current_date += timedelta(days=1)
    return pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(timestamps),
        columns=["open", "high", "low", "close", "volume"],
    )


class WalkForwardTests(unittest.TestCase):
    def test_rolling_folds_are_chronological_disjoint_and_train_only(self):
        frame = synthetic_sessions(12)
        config = TradingConfig(
            interval="5m",
            opening_range_minutes=15,
            stop_loss_pct=0.004,
            take_profit_pct=0.008,
            volume_spike_threshold=1.2,
            starting_capital=100000,
            risk_per_trade_pct=0.01,
            max_trades_per_day=1,
        )
        result = walk_forward_validate(
            frame,
            config,
            WalkForwardConfig(
                train_sessions=4,
                test_sessions=2,
                step_sessions=2,
                gap_sessions=1,
                mode="rolling",
            ),
            parameter_grid={
                "opening_range_minutes": (10, 15),
                "stop_loss_pct": (0.004,),
                "take_profit_pct": (0.008,),
                "volume_spike_threshold": (1.2,),
                "volume_ma_window": (20,),
                "vwap_price_source": ("typical",),
            },
        )

        evaluated = [
            fold for fold in result.folds if fold.status == "evaluated"]
        self.assertGreaterEqual(len(evaluated), 2)
        test_dates = []
        for fold in evaluated:
            self.assertLess(fold.train_end, fold.test_start)
            self.assertIsNotNone(fold.selected_parameters)
            self.assertIsNotNone(fold.train_metrics)
            self.assertIsNotNone(fold.test_metrics)
            test_dates.extend(pd.to_datetime(
                fold.test_trades["exit_ts"]).dt.date.tolist())
        self.assertEqual(len(test_dates), len(set(test_dates)))
        self.assertIn("status", result.aggregate_oos_metrics)
        self.assertGreater(len(result.oos_equity_curve), 0)

    def test_expanding_training_window_grows_between_folds(self):
        frame = synthetic_sessions(10)
        config = TradingConfig(max_trades_per_day=1)
        seen_training_dates = []

        original_run = __import__("stockmarket.backtest", fromlist=[
                                  "run_backtest"]).run_backtest

        def tracking_run(data, cfg, **kwargs):
            seen_training_dates.append(tuple(sorted(set(data.index.date))))
            return original_run(data, cfg, **kwargs)

        with patch("stockmarket.validation.walk_forward.run_backtest", side_effect=tracking_run):
            walk_forward_validate(
                frame,
                config,
                WalkForwardConfig(
                    train_sessions=3,
                    test_sessions=1,
                    step_sessions=1,
                    mode="expanding",
                    min_train_trades=1,
                ),
            )
        # For each fold, the training simulation is the first call; each next
        # training slice expands while test data advances one session.
        training_lengths = [len(dates) for dates in seen_training_dates[::2]]
        self.assertGreaterEqual(len(training_lengths), 2)
        self.assertTrue(all(a < b for a, b in zip(
            training_lengths, training_lengths[1:])))

    def test_reports_skipped_fold_when_training_has_too_few_trades(self):
        frame = synthetic_sessions(8)
        config = TradingConfig(volume_spike_threshold=100.0)
        result = walk_forward_validate(
            frame,
            config,
            WalkForwardConfig(
                train_sessions=3,
                test_sessions=2,
                step_sessions=2,
                min_train_trades=2,
            ),
        )
        self.assertTrue(result.folds)
        self.assertTrue(all(fold.status == "skipped" for fold in result.folds))
        self.assertTrue(all(fold.reason for fold in result.folds))
        self.assertEqual(
            result.aggregate_oos_metrics["status"], "no_evaluated_oos_folds")

    def test_rejects_overlapping_oos_step_and_invalid_parameter_name(self):
        with self.assertRaisesRegex(ValueError, "step_sessions"):
            WalkForwardConfig(train_sessions=3,
                              test_sessions=2, step_sessions=1)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            walk_forward_validate(
                synthetic_sessions(6),
                TradingConfig(),
                WalkForwardConfig(train_sessions=2,
                                  test_sessions=1, step_sessions=1),
                parameter_grid={"starting_capital": (1000,)},
            )

    def test_rejects_data_too_short_for_one_fold(self):
        with self.assertRaisesRegex(ValueError, "Need at least"):
            walk_forward_validate(
                synthetic_sessions(4),
                TradingConfig(),
                WalkForwardConfig(
                    train_sessions=3,
                    test_sessions=2,
                    step_sessions=2,
                ),
            )


if __name__ == "__main__":
    unittest.main()
