"""Ranking / scoring from extracted module."""

from __future__ import annotations

import pandas as pd
import pytest

from stockmarket.ranking import score_signal


def _row(**kw) -> pd.Series:
    base = {
        "open": 100.0,
        "close": 101.0,
        "vwap": 99.0,
        "or_high": 102.0,
        "or_low": 98.0,
        "vol_spike": False,
        "long_signal": False,
        "short_signal": False,
    }
    base.update(kw)
    return pd.Series(base)


def test_orb_long_signal_scores_high():
    row = _row(vol_spike=True, long_signal=True, close=105.0, or_high=104.0)
    score, action, bias = score_signal(row, "ORB + VWAP", allow_short=False)
    assert score >= 100
    assert action == "LONG NOW"


def test_vwap_trend_action():
    row = _row(vol_spike=True, close=110.0, vwap=100.0)
    score, action, _ = score_signal(row, "VWAP Trend", allow_short=False)
    assert action == "BUY TREND"
