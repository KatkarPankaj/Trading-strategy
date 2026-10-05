from __future__ import annotations

import pandas as pd

from .config import TradingConfig
from .core.market_session import MarketSession


def _interval_minutes(interval: str) -> int:
    if not interval.endswith("m"):
        raise ValueError(
            f"Only minute intervals are supported in this MVP. Got: {interval}")
    return int(interval[:-1])


def add_strategy_columns(df: pd.DataFrame, cfg: TradingConfig) -> pd.DataFrame:
    out = df.copy()

    out["date"] = out.index.date
    if cfg.vwap_price_source == "typical":
        out["vwap_price"] = (out["high"] + out["low"] + out["close"]) / 3.0
    elif cfg.vwap_price_source == "close":
        out["vwap_price"] = out["close"]
    else:
        raise ValueError("vwap_price_source must be 'typical' or 'close'")

    # VWAP is session-reset daily.
    out["cum_tpv"] = (out["vwap_price"] * out["volume"]
                      ).groupby(out["date"]).cumsum()
    out["cum_vol"] = out["volume"].groupby(out["date"]).cumsum()
    out["vwap"] = out["cum_tpv"] / out["cum_vol"].where(out["cum_vol"] != 0)

    out["vol_ma"] = out["volume"].rolling(
        cfg.volume_ma_window, min_periods=1).mean()
    out["vol_spike"] = out["volume"] >= (
        out["vol_ma"] * cfg.volume_spike_threshold)

    bars_in_range = max(1, cfg.opening_range_minutes //
                        _interval_minutes(cfg.interval))

    opening_high = []
    opening_low = []
    valid_after_open = []

    for _, day_df in out.groupby("date", sort=True):
        or_df = day_df.head(bars_in_range)
        or_high_val = float(or_df["high"].max())
        or_low_val = float(or_df["low"].min())

        opening_high.extend([or_high_val] * len(day_df))
        opening_low.extend([or_low_val] * len(day_df))

        or_last_ts = or_df.index[-1]
        valid_after_open.extend([ts > or_last_ts for ts in day_df.index])

    out["or_high"] = opening_high
    out["or_low"] = opening_low
    out["valid_after_open"] = valid_after_open

    session = MarketSession.from_config(cfg)
    out["before_cutoff"] = [
        session.is_before_entry_cutoff(ts.to_pydatetime()) for ts in out.index
    ]

    close_above_vwap = out["close"].gt(out["vwap"]).fillna(False)
    close_below_vwap = out["close"].lt(out["vwap"]).fillna(False)

    out["long_signal"] = (
        out["valid_after_open"]
        & out["before_cutoff"]
        & out["vol_spike"]
        & (out["close"] > out["or_high"])
        & close_above_vwap
    )

    out["short_signal"] = (
        cfg.allow_short
        & out["valid_after_open"]
        & out["before_cutoff"]
        & out["vol_spike"]
        & (out["close"] < out["or_low"])
        & close_below_vwap
    )

    return out
