import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from stockmarket.config import TradingConfig
from stockmarket.validation.robustness import (
    build_robustness_scenarios,
    default_parameter_variations,
    run_robustness_analysis,
)
from stockmarket.validation.walk_forward import WalkForwardConfig


def small_market_frame(session_count=8):
    index = []
    rows = []
    day = date(2026, 2, 2)
    while len(set(ts.date() for ts in index)) < session_count:
        if day.weekday() < 5:
            for bar in range(6):
                index.append(
                    datetime.combine(day, time(9, 15)).replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")
                    ) + timedelta(minutes=5 * bar)
                )
                if bar < 3:
                    rows.append((100.0, 100.5, 99.5, 100.0, 100.0))
                elif bar == 3:
                    rows.append((100.5, 102.0, 100.5, 101.0, 500.0))
                elif bar == 4:
                    rows.append((101.0, 101.1, 100.5, 100.8, 100.0))
                else:
                    rows.append((100.8, 101.0, 100.7, 100.9, 100.0))
        day += timedelta(days=1)
    return pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(index),
        columns=["open", "high", "low", "close", "volume"],
    )


class RobustnessScenarioTests(unittest.TestCase):
    def test_baseline_and_one_factor_parameter_scenarios_are_deterministic(self):
        config = TradingConfig()
        variations = {
            "opening_range_minutes": (10, 15, 20),
            "stop_loss_pct": (0.002, 0.004, 0.006),
            "vwap_price_source": ("typical", "close"),
        }
        first = build_robustness_scenarios(
            config,
            parameter_variations=variations,
            commission_multipliers=(1.0,),
            slippage_multipliers=(1.0,),
        )
        second = build_robustness_scenarios(
            config,
            parameter_variations=variations,
            commission_multipliers=(1.0,),
            slippage_multipliers=(1.0,),
        )
        self.assertEqual([scenario.scenario_id for scenario in first],
                         [scenario.scenario_id for scenario in second])
        self.assertEqual(first[0].scenario_id, "baseline")
        self.assertEqual(first[0].config, config)
        for scenario in first[1:]:
            changed = [
                name
                for name in (
                    "opening_range_minutes",
                    "stop_loss_pct",
                    "take_profit_pct",
                    "volume_spike_threshold",
                    "volume_ma_window",
                    "vwap_price_source",
                )
                if getattr(scenario.config, name) != getattr(config, name)
            ]
            self.assertEqual(len(changed), 1, scenario.scenario_id)

    def test_default_robustness_variants_cover_requested_dimensions(self):
        variations = default_parameter_variations(TradingConfig())
        self.assertIn("opening_range_minutes", variations)
        self.assertIn("stop_loss_pct", variations)
        self.assertIn("take_profit_pct", variations)
        self.assertIn("volume_spike_threshold", variations)
        self.assertIn("vwap_price_source", variations)
        self.assertIn("close", variations["vwap_price_source"])

    def test_cost_sensitivities_are_separate_scenarios(self):
        scenarios = build_robustness_scenarios(
            TradingConfig(),
            parameter_variations={},
            commission_multipliers=(0.5, 1.0, 1.5),
            slippage_multipliers=(0.5, 1.0, 1.5),
        )
        by_id = {scenario.scenario_id: scenario for scenario in scenarios}
        self.assertEqual(by_id["commission_x0.5"].commission_multiplier, 0.5)
        self.assertEqual(by_id["commission_x1.5"].commission_multiplier, 1.5)
        self.assertEqual(by_id["slippage_x0.5"].slippage_multiplier, 0.5)
        self.assertEqual(by_id["slippage_x1.5"].slippage_multiplier, 1.5)

    def test_rejects_too_many_or_unknown_scenarios(self):
        with self.assertRaisesRegex(ValueError, "max_scenarios"):
            build_robustness_scenarios(
                TradingConfig(),
                parameter_variations={"opening_range_minutes": (5, 10, 20)},
                commission_multipliers=(0.5, 1.0, 1.5),
                slippage_multipliers=(0.5, 1.0, 1.5),
                max_scenarios=2,
            )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            build_robustness_scenarios(
                TradingConfig(),
                parameter_variations={"risk_per_trade_pct": (0.01,)},
            )

    def test_runner_returns_one_oos_result_per_fixed_scenario(self):
        result = run_robustness_analysis(
            small_market_frame(),
            TradingConfig(max_trades_per_day=1),
            WalkForwardConfig(
                train_sessions=3,
                test_sessions=1,
                step_sessions=1,
                min_train_trades=1,
            ),
            parameter_variations={"opening_range_minutes": (15, 20)},
            commission_multipliers=(1.0,),
            slippage_multipliers=(1.0, 1.5),
            max_scenarios=5,
        )
        self.assertEqual(len(result.scenarios), 3)
        self.assertEqual(result.scenarios[0].scenario.scenario_id, "baseline")
        self.assertTrue(all(s.walk_forward.folds for s in result.scenarios))
        summary = result.summary_frame()
        self.assertIn("scenario_id", summary.columns)
        self.assertIn("total_return", summary.columns)


if __name__ == "__main__":
    unittest.main()
