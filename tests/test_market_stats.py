from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase

import numpy as np
import pandas as pd

from stockmarket.core.market_stats import MarketStatsProvider, MarketStatsSettings
from stockmarket.core.models import AssetClass, Instrument, TradingStatus

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)  # Monday, US session open


def _instrument(symbol: str) -> Instrument:
    return Instrument(
        instrument_id=f"XNAS:{symbol}", symbol=symbol, exchange="NASDAQ", market="US",
        asset_class=AssetClass.EQUITY, currency="USD", timezone="America/New_York",
        tick_size=0.01, trading_status=TradingStatus.ACTIVE)


A, B, C = _instrument("AAA"), _instrument("BBB"), _instrument("CCC")
INSTRUMENTS = {i.instrument_id: i for i in (A, B, C)}


def _bars(closes, volume=1_000_000.0, *, include_today=False):
    days = pd.bdate_range(end=NOW.date() - timedelta(days=1), periods=len(closes))
    index = pd.DatetimeIndex([d + pd.Timedelta(hours=20) for d in days], tz="UTC")
    df = pd.DataFrame({"close": closes, "volume": volume}, index=index)
    if include_today:
        today = pd.DataFrame({"close": [1e9], "volume": [1e12]},
                             index=pd.DatetimeIndex([NOW], tz="UTC"))
        df = pd.concat([df, today])
    return df


class _Data:
    def __init__(self, frames):
        self.frames = frames
        self.calls = 0

    def get_ohlcv(self, instrument, interval, start, end):
        self.calls += 1
        assert interval == "1d" and start.tzinfo and end.tzinfo
        frame = self.frames[instrument.instrument_id]
        if isinstance(frame, Exception):
            raise frame
        return frame


class _Portfolio:
    def __init__(self, positions=None, rates=None):
        self._positions = positions or {}
        self.rates = rates if rates is not None else {"USD": 1.0}

    def positions(self):
        return self._positions

    def rate_to_base(self, currency):
        return self.rates[currency]


def _held(quantity, price, currency="USD"):
    return SimpleNamespace(quantity=quantity, last_price=price, currency=currency)


rng = np.random.default_rng(7)
BASE = 100 * np.cumprod(1 + rng.normal(0, 0.01, 20))
NOISE = 100 * np.cumprod(1 + rng.normal(0, 0.01, 20))


class MarketStatsTests(TestCase):
    def _provider(self, frames, portfolio=None, **settings):
        data = _Data(frames)
        provider = MarketStatsProvider(
            market_data=data, instruments=INSTRUMENTS, portfolio=portfolio or _Portfolio(),
            settings=MarketStatsSettings(**settings), clock=lambda: NOW)
        return provider, data

    def test_adv_and_slippage_from_completed_daily_bars(self):
        provider, _ = self._provider({A.instrument_id: _bars(BASE, include_today=True)})

        stats = provider(A.instrument_id)

        expected_adv = float((BASE * 1_000_000.0).mean())
        self.assertAlmostEqual(stats["average_daily_traded_value"], expected_adv, places=2)
        sigma = pd.Series(BASE).pct_change().dropna().std(ddof=1)
        impact = sigma * np.sqrt(25_000.0 / expected_adv) * 10_000
        self.assertAlmostEqual(stats["expected_slippage_bps"], round(2.0 + impact, 4), places=4)
        self.assertEqual(stats["correlated_exposure"], 0.0)

    def test_fails_closed_on_short_history_errors_unknown_fx_and_unknown_instruments(self):
        short, _ = self._provider({A.instrument_id: _bars(BASE[:10])})
        self.assertEqual(short(A.instrument_id), {})
        broken, _ = self._provider({A.instrument_id: RuntimeError("rate limited")})
        self.assertEqual(broken(A.instrument_id), {})
        no_fx, _ = self._provider({A.instrument_id: _bars(BASE)}, _Portfolio(rates={}))
        self.assertEqual(no_fx(A.instrument_id), {"correlated_exposure": 0.0})
        self.assertEqual(short("XNAS:UNKNOWN"), {})

    def test_correlated_exposure_sums_only_highly_correlated_holdings(self):
        frames = {A.instrument_id: _bars(BASE), B.instrument_id: _bars(BASE * 1.5),
                  C.instrument_id: _bars(NOISE)}
        portfolio = _Portfolio({B.instrument_id: _held(10, 150.0),
                                C.instrument_id: _held(5, 100.0)})
        provider, _ = self._provider(frames, portfolio)

        self.assertAlmostEqual(provider(A.instrument_id)["correlated_exposure"], 1_500.0)

    def test_missing_holding_data_makes_correlation_unknown(self):
        frames = {A.instrument_id: _bars(BASE), B.instrument_id: _bars(BASE[:5])}
        provider, _ = self._provider(frames, _Portfolio({B.instrument_id: _held(10, 150.0)}))

        stats = provider(A.instrument_id)

        self.assertNotIn("correlated_exposure", stats)
        self.assertIn("average_daily_traded_value", stats)

    def test_bars_are_cached_per_day_but_failures_are_retried(self):
        provider, data = self._provider({A.instrument_id: _bars(BASE)})
        provider(A.instrument_id)
        provider(A.instrument_id)
        self.assertEqual(data.calls, 1)
        failing, data = self._provider({A.instrument_id: RuntimeError("timeout")})
        failing(A.instrument_id)
        failing(A.instrument_id)
        self.assertEqual(data.calls, 2)

    def test_settings_are_validated(self):
        for bad in ({"lookback_days": 2}, {"correlation_threshold": 0.0},
                    {"half_spread_bps": float("nan")}, {"reference_order_notional": -1.0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MarketStatsSettings(**bad)
