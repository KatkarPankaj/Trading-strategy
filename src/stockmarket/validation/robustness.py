"""Deterministic one-factor-at-a-time robustness analysis."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Mapping, Sequence

import pandas as pd

from ..config import TradingConfig
from .walk_forward import WalkForwardConfig, WalkForwardResult, walk_forward_validate


@dataclass(frozen=True, slots=True)
class RobustnessScenario:
    scenario_id: str
    name: str
    config: TradingConfig
    commission_multiplier: float = 1.0
    slippage_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class RobustnessScenarioResult:
    scenario: RobustnessScenario
    walk_forward: WalkForwardResult


@dataclass(frozen=True, slots=True)
class RobustnessResult:
    scenarios: tuple[RobustnessScenarioResult, ...]

    def summary_frame(self) -> pd.DataFrame:
        rows = []
        for item in self.scenarios:
            row = {
                "scenario_id": item.scenario.scenario_id,
                "scenario": item.scenario.name,
                "commission_multiplier": item.scenario.commission_multiplier,
                "slippage_multiplier": item.scenario.slippage_multiplier,
                **_scenario_parameters(item.scenario.config),
                **item.walk_forward.aggregate_oos_metrics,
            }
            rows.append(row)
        return pd.DataFrame(rows)


def build_robustness_scenarios(
    base_config: TradingConfig,
    *,
    parameter_variations: Mapping[str,
                                  Sequence[int | float | str]] | None = None,
    commission_multipliers: Sequence[float] = (0.5, 1.0, 1.5),
    slippage_multipliers: Sequence[float] = (0.5, 1.0, 1.5),
    max_scenarios: int = 100,
) -> tuple[RobustnessScenario, ...]:
    """Build stable baseline and single-parameter perturbation scenarios."""
    if not isinstance(base_config, TradingConfig):
        raise TypeError("base_config must be a TradingConfig")
    if isinstance(max_scenarios, bool) or not isinstance(max_scenarios, int):
        raise TypeError("max_scenarios must be an integer")
    if max_scenarios < 1:
        raise ValueError("max_scenarios must be positive")

    variations = dict(parameter_variations or {})
    allowed = {
        "opening_range_minutes",
        "stop_loss_pct",
        "take_profit_pct",
        "volume_spike_threshold",
        "volume_ma_window",
        "vwap_price_source",
    }
    unknown = set(variations).difference(allowed)
    if unknown:
        raise ValueError(
            f"Unsupported robustness parameters: {sorted(unknown)}")

    scenarios: list[RobustnessScenario] = [
        RobustnessScenario("baseline", "baseline", base_config)
    ]
    for parameter in sorted(variations):
        for value in _unique(variations[parameter]):
            if value == getattr(base_config, parameter):
                continue
            candidate = replace(base_config, **{parameter: value})
            scenarios.append(
                RobustnessScenario(
                    scenario_id=f"{parameter}={value}",
                    name=f"perturb_{parameter}",
                    config=candidate,
                )
            )

    for value in _unique_numeric(commission_multipliers, "commission_multipliers"):
        if value == 1.0:
            continue
        scenarios.append(
            RobustnessScenario(
                scenario_id=f"commission_x{value:g}",
                name="commission_sensitivity",
                config=base_config,
                commission_multiplier=value,
            )
        )
    for value in _unique_numeric(slippage_multipliers, "slippage_multipliers"):
        if value == 1.0:
            continue
        scenarios.append(
            RobustnessScenario(
                scenario_id=f"slippage_x{value:g}",
                name="slippage_sensitivity",
                config=base_config,
                slippage_multiplier=value,
            )
        )

    if len(scenarios) > max_scenarios:
        raise ValueError(
            f"Generated {len(scenarios)} scenarios, above max_scenarios={max_scenarios}"
        )
    return tuple(scenarios)


def run_robustness_analysis(
    df: pd.DataFrame,
    base_config: TradingConfig,
    walk_forward_config: WalkForwardConfig,
    *,
    parameter_variations: Mapping[str,
                                  Sequence[int | float | str]] | None = None,
    commission_multipliers: Sequence[float] = (0.5, 1.0, 1.5),
    slippage_multipliers: Sequence[float] = (0.5, 1.0, 1.5),
    max_scenarios: int = 100,
) -> RobustnessResult:
    scenarios = build_robustness_scenarios(
        base_config,
        parameter_variations=parameter_variations,
        commission_multipliers=commission_multipliers,
        slippage_multipliers=slippage_multipliers,
        max_scenarios=max_scenarios,
    )
    results = []
    for scenario in scenarios:
        walk_forward = walk_forward_validate(
            df,
            scenario.config,
            walk_forward_config,
            commission_multiplier=scenario.commission_multiplier,
            slippage_multiplier=scenario.slippage_multiplier,
        )
        results.append(RobustnessScenarioResult(scenario, walk_forward))
    return RobustnessResult(tuple(results))


def default_parameter_variations(
    config: TradingConfig,
) -> dict[str, tuple[int | float | str, ...]]:
    """Return conservative OAT variants around the configured baseline."""
    return {
        "opening_range_minutes": tuple(
            value for value in (10, 15, 20)
            if value != config.opening_range_minutes
        ),
        "stop_loss_pct": tuple(
            value for value in _positive_scale(config.stop_loss_pct, (0.5, 1.5))
            if value != config.stop_loss_pct
        ),
        "take_profit_pct": tuple(
            value for value in _positive_scale(config.take_profit_pct, (0.5, 1.5))
            if value != config.take_profit_pct
        ),
        "volume_spike_threshold": tuple(
            value for value in _positive_scale(config.volume_spike_threshold, (0.8, 1.2))
            if value != config.volume_spike_threshold
        ),
        "volume_ma_window": tuple(
            value for value in (10, 20, 30)
            if value != config.volume_ma_window
        ),
        "vwap_price_source": tuple(
            value for value in ("typical", "close")
            if value != config.vwap_price_source
        ),
    }


def _scenario_parameters(config: TradingConfig) -> dict[str, Any]:
    return {
        "opening_range_minutes": config.opening_range_minutes,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "volume_spike_threshold": config.volume_spike_threshold,
        "volume_ma_window": config.volume_ma_window,
        "vwap_price_source": config.vwap_price_source,
    }


def _positive_scale(value: float, multipliers: Sequence[float]) -> tuple[float, ...]:
    return tuple(float(value) * multiplier for multiplier in multipliers)


def _unique(values: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(dict.fromkeys(values))


def _unique_numeric(values: Sequence[float], field_name: str) -> tuple[float, ...]:
    normalized = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{field_name} must contain numbers")
        if not isfinite(value) or value < 0:
            raise ValueError(
                f"{field_name} values must be finite and non-negative")
        normalized.append(float(value))
    return tuple(dict.fromkeys(normalized))
