from __future__ import annotations

from dataclasses import replace
from itertools import product
from typing import Iterable

import pandas as pd

from .backtest import run_backtest
from .config import TradingConfig


def _unique_sorted(values: Iterable[float]) -> list[float]:
    return sorted(set(float(v) for v in values))


def run_parameter_sweep(
    df: pd.DataFrame,
    base_cfg: TradingConfig,
    opening_ranges: Iterable[int],
    stop_losses: Iterable[float],
    take_profits: Iterable[float],
    volume_spikes: Iterable[float],
) -> pd.DataFrame:
    rows: list[dict[str, float]] = []

    range_values = sorted(set(int(v) for v in opening_ranges))
    stop_values = _unique_sorted(stop_losses)
    target_values = _unique_sorted(take_profits)
    spike_values = _unique_sorted(volume_spikes)

    for opening_range, stop_loss, take_profit, volume_spike in product(
        range_values, stop_values, target_values, spike_values
    ):
        cfg = replace(
            base_cfg,
            opening_range_minutes=opening_range,
            stop_loss_pct=stop_loss,
            take_profit_pct=take_profit,
            volume_spike_threshold=volume_spike,
        )

        result = run_backtest(df, cfg)
        row = {
            "opening_range_minutes": float(opening_range),
            "stop_loss_pct": stop_loss,
            "take_profit_pct": take_profit,
            "volume_spike_threshold": volume_spike,
            "total_trades": result.summary["total_trades"],
            "win_rate": result.summary["win_rate"],
            "net_pnl": result.summary["net_pnl"],
            "return_pct": result.summary["return_pct"],
            "max_drawdown_pct": result.summary["max_drawdown_pct"],
            "profit_factor": result.summary["profit_factor"],
        }
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = out.sort_values(
        by=["return_pct", "net_pnl", "profit_factor", "max_drawdown_pct"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    return out
