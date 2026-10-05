import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from stockmarket.cli import _summarize_observed_data
from stockmarket.config import TradingConfig
from stockmarket.validation.reports import write_validation_report
from stockmarket.validation.robustness import (
    RobustnessResult,
    RobustnessScenario,
    RobustnessScenarioResult,
)
from stockmarket.validation.walk_forward import (
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardResult,
)


def sample_walk_forward():
    fold = WalkForwardFold(
        fold=1,
        status="evaluated",
        train_start="2026-01-01",
        train_end="2026-01-10",
        test_start="2026-01-13",
        test_end="2026-01-15",
        selected_parameters={"opening_range_minutes": 15},
        train_metrics={"total_return": 0.1, "sharpe_ratio": 1.2},
        test_metrics={
            "total_trades": 2,
            "total_return": -0.02,
            "sharpe_ratio": None,
            "sharpe_status": "undefined_insufficient_daily_returns",
        },
        test_trades=pd.DataFrame({"net_pnl": [-10.0, 5.0], "fold": [1, 1]}),
        test_equity_curve=pd.DataFrame(
            columns=["timestamp", "equity", "position_open"]
        ),
    )
    return WalkForwardResult(
        folds=(fold,),
        aggregate_oos_metrics={
            "total_trades": 2,
            "total_return": -0.02,
            "sharpe_ratio": None,
            "status": "evaluated",
        },
        oos_trades=fold.test_trades,
        oos_equity_curve=fold.test_equity_curve,
        config=WalkForwardConfig(
            train_sessions=10,
            test_sessions=3,
            step_sessions=3,
        ),
    )


class ValidationReportTests(unittest.TestCase):
    def test_writes_strict_json_csv_and_markdown_with_is_oos_split(self):
        walk_forward = sample_walk_forward()
        robustness = RobustnessResult(
            scenarios=(
                RobustnessScenarioResult(
                    scenario=RobustnessScenario(
                        "baseline", "baseline", TradingConfig()
                    ),
                    walk_forward=walk_forward,
                ),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_validation_report(
                temp_dir,
                symbol="TEST.NS",
                config=TradingConfig(),
                walk_forward=walk_forward,
                robustness=robustness,
                data_summary={
                    "observations": 4,
                    "observed_sessions": 1,
                    "missing_in_session_bars": 1,
                    "first_timestamp": "2026-01-01T09:15:00+05:30",
                    "last_timestamp": "2026-01-01T09:35:00+05:30",
                },
                report_id="fold-test",
            )
            self.assertTrue(all(path.exists() for path in paths.values()))
            content = paths["json"].read_text(encoding="utf-8")
            document = json.loads(
                content, parse_constant=lambda value: self.fail(value))
            self.assertIsNone(
                document["out_of_sample_evaluation"]["aggregate_metrics"]["sharpe_ratio"]
            )
            self.assertFalse(document["methodology"]
                             ["selection_uses_test_data"])
            self.assertEqual(document["data_summary"]
                             ["missing_in_session_bars"], 1)
            self.assertEqual(
                document["out_of_sample_evaluation"]["folds"][0]["train_end"],
                "2026-01-10",
            )

            rows = pd.read_csv(paths["csv"])
            self.assertIn("train_total_return", rows.columns)
            self.assertIn("oos_total_return", rows.columns)
            self.assertEqual(rows.iloc[0]["report_type"], "walk_forward")

            markdown = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("In-sample selection", markdown)
            self.assertIn("Out-of-sample aggregate", markdown)
            self.assertIn("Robustness scenarios", markdown)

    def test_rejects_empty_symbol_or_report_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                write_validation_report(
                    temp_dir,
                    symbol=" ",
                    config=TradingConfig(),
                    walk_forward=sample_walk_forward(),
                )

    def test_gap_summary_only_counts_within_observed_session_dates(self):
        index = pd.DatetimeIndex(
            [
                "2026-01-05 09:15:00+05:30",
                "2026-01-05 09:20:00+05:30",
                "2026-01-05 09:30:00+05:30",
                "2026-01-06 09:15:00+05:30",
            ]
        )
        summary = _summarize_observed_data(
            pd.DataFrame(index=index), TradingConfig(interval="5m")
        )
        self.assertEqual(summary["observations"], 4)
        self.assertEqual(summary["observed_sessions"], 2)
        self.assertEqual(summary["missing_in_session_bars"], 1)


if __name__ == "__main__":
    unittest.main()
