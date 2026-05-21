"""Backtest legacy vs cycle pipeline parity (BACKTEST_USE_CYCLE)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from stockmarket.backtest import BacktestResult, run_backtest
from stockmarket.config import TradingConfig

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden"
GOLDEN_TRADES = FIXTURE_DIR / "backtest_trades_synthetic.csv"
GOLDEN_SUMMARY = FIXTURE_DIR / "backtest_summary_synthetic.json"


def _synthetic_intraday_df() -> pd.DataFrame:
    """Two-session bars engineered for one long entry and stop exit."""
    rows = []
    base = pd.Timestamp("2026-05-12 09:15:00")
    or_bars = [
        (100.0, 101.0, 99.5, 100.5, 50_000),
        (100.5, 102.0, 100.0, 101.5, 45_000),
        (101.0, 102.5, 100.5, 102.0, 40_000),
    ]
    for i, (o, h, l, c, v) in enumerate(or_bars):
        rows.append((base + pd.Timedelta(minutes=5 * i), o, h, l, c, v))

    signal_ts = base + pd.Timedelta(minutes=20)
    rows.append((signal_ts, 103.0, 104.0, 102.8, 103.5, 120_000))

    exit_ts = base + pd.Timedelta(minutes=25)
    rows.append((exit_ts, 103.0, 103.2, 100.0, 101.0, 80_000))

    for i in range(6, 20):
        ts = base + pd.Timedelta(minutes=5 * i)
        rows.append((ts, 101.0, 101.5, 100.5, 101.0, 30_000))

    day2 = pd.Timestamp("2026-05-13 09:15:00")
    for i in range(20):
        ts = day2 + pd.Timedelta(minutes=5 * i)
        rows.append((ts, 100.0 + i * 0.1, 101.0, 99.0, 100.5, 25_000))

    idx, o, h, l, c, v = zip(*rows)
    return pd.DataFrame(
        {"open": o, "high": h, "low": l, "close": c, "volume": v},
        index=pd.DatetimeIndex(idx, name="Datetime"),
    )


def _cfg() -> TradingConfig:
    return TradingConfig(
        symbol="TEST.NS",
        interval="5m",
        period="5d",
        opening_range_minutes=15,
        entry_cutoff_time="13:30",
        square_off_time="15:15",
        time_exit_minutes=120,
        stop_loss_pct=0.004,
        take_profit_pct=0.008,
        risk_per_trade_pct=0.005,
        starting_capital=200_000.0,
        max_trades_per_day=1,
        commission_pct=0.0003,
        slippage_pct=0.0005,
        allow_short=False,
        volume_ma_window=3,
        volume_spike_threshold=1.2,
    )


def _write_golden(result: BacktestResult) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    result.trades.to_csv(GOLDEN_TRADES, index=False)
    GOLDEN_SUMMARY.write_text(json.dumps(result.summary, indent=2), encoding="utf-8")


@pytest.fixture
def synthetic_df() -> pd.DataFrame:
    return _synthetic_intraday_df()


def test_legacy_backtest_matches_golden(synthetic_df: pd.DataFrame):
    if not GOLDEN_TRADES.exists():
        _write_golden(run_backtest(synthetic_df, _cfg()))

    golden = pd.read_csv(GOLDEN_TRADES)
    legacy = run_backtest(synthetic_df, _cfg()).trades
    assert len(legacy) == len(golden)
    if len(legacy):
        assert float(legacy["net_pnl"].sum()) == pytest.approx(
            float(golden["net_pnl"].sum()), rel=1e-9, abs=1e-6
        )


def test_cycle_backtest_parity_with_legacy(synthetic_df: pd.DataFrame, monkeypatch):
    monkeypatch.delenv("BACKTEST_USE_CYCLE", raising=False)
    legacy = run_backtest(synthetic_df, _cfg()).trades
    monkeypatch.setenv("BACKTEST_USE_CYCLE", "1")
    cycle = run_backtest(synthetic_df, _cfg()).trades

    assert len(cycle) == len(legacy)
    if legacy.empty:
        return

    for col in ("net_pnl", "qty", "entry_price", "exit_price"):
        assert cycle[col].tolist() == pytest.approx(
            legacy[col].tolist(), rel=1e-9, abs=1e-4
        )
    assert cycle["exit_reason"].tolist() == legacy["exit_reason"].tolist()
