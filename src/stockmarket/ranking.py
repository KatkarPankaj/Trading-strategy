"""Ranking and scoring logic extracted from ``dashboard.py`` for tests."""

from __future__ import annotations

import pandas as pd


def score_signal(
    last_row: pd.Series, mode: str, allow_short: bool
) -> tuple[float, str, str]:
    close = float(last_row["close"])
    open_ = float(last_row["open"])
    vwap = float(last_row["vwap"])
    or_high = float(last_row["or_high"])
    or_low = float(last_row["or_low"])
    vol_spike = bool(last_row["vol_spike"])
    long_signal = bool(last_row["long_signal"])
    short_signal = bool(last_row["short_signal"])

    bias = "Bullish" if close > vwap else "Bearish"
    score = 0.0
    action = "WAIT"

    if mode == "ORB + VWAP":
        score += 45 if vol_spike else 5
        score += 25 if close > vwap else 0
        score += 20 if close > open_ else 0

        dist_to_high_pct = abs(or_high - close) / max(close, 1e-6) * 100
        score += max(0.0, 15 - dist_to_high_pct * 8)

        if long_signal:
            score += 70
            action = "LONG NOW"
        elif allow_short and short_signal:
            score += 70
            action = "SHORT NOW"
        elif close > vwap and close >= 0.997 * or_high:
            action = "WATCH LONG"
        elif allow_short and close < vwap and close <= 1.003 * or_low:
            action = "WATCH SHORT"

    elif mode == "VWAP Trend":
        vwap_gap_pct = abs(close - vwap) / max(vwap, 1e-6) * 100
        score += min(35.0, vwap_gap_pct * 40)
        score += 30 if vol_spike else 8
        score += 20 if close > open_ else 10

        if close > vwap and vol_spike:
            action = "BUY TREND"
            score += 30
        elif allow_short and close < vwap and vol_spike:
            action = "SELL TREND"
            score += 30

    else:  # OR Reversal
        near_low = close <= or_low * 1.002
        near_high = close >= or_high * 0.998
        score += 35 if vol_spike else 8
        score += 20 if near_low or near_high else 5

        if near_low and close > vwap:
            action = "BUY REVERSAL"
            score += 45
        elif allow_short and near_high and close < vwap:
            action = "SELL REVERSAL"
            score += 45

    return round(score, 2), action, bias
