"""batch_fetch_intraday_data fetches per symbol (Finnhub US, NSE path for India)."""

from __future__ import annotations

import pandas as pd

import stockmarket.data as data


def test_batch_fetch_per_symbol(monkeypatch):
    ix = pd.date_range("2024-01-02 09:15", periods=3, freq="5min", tz="Asia/Kolkata")

    def fake_fetch(
        symbol: str,
        interval: str,
        period: str,
        tz: str = "Asia/Kolkata",
        max_retries: int = 4,
        backoff_base: float = 5.0,
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": [100.0, 100.5, 101.0],
                "high": [101.0, 101.5, 102.0],
                "low": [99.5, 100.0, 100.5],
                "close": [100.5, 101.0, 101.5],
                "volume": [1000.0, 1100.0, 900.0],
            },
            index=ix.tz_convert("UTC"),
        )

    monkeypatch.setattr(data, "fetch_intraday_data", fake_fetch)
    monkeypatch.setattr(data, "_load_cache", lambda *a, **k: None)

    m, errs = data.batch_fetch_intraday_data(
        ["RELIANCE.NS"],
        interval="5m",
        period="5d",
        tz="Asia/Kolkata",
        max_retries=1,
    )
    assert "RELIANCE.NS" in m
    assert len(errs) == 0
    assert not m["RELIANCE.NS"].empty
