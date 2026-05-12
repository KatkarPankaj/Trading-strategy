"""batch_fetch_intraday_data uses one download for multiple symbols."""

from __future__ import annotations

import pandas as pd

import stockmarket.data as data


def test_batch_fetch_splits_multiindex(monkeypatch):
    ix = pd.date_range("2024-01-02 09:15", periods=3, freq="5min", tz="UTC")
    cols = pd.MultiIndex.from_product(
        [["RELIANCE.NS"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    raw = pd.DataFrame(
        [
            [100, 101, 99, 100.5, 1000],
            [100.5, 102, 100, 101.5, 1200],
            [101.5, 103, 101, 102.0, 900],
        ],
        index=ix,
        columns=cols,
    )

    def fake_download(**kwargs):
        return raw

    monkeypatch.setattr(data.yf, "download", fake_download)
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
