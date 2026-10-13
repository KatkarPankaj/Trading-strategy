from datetime import datetime, timedelta, timezone
import unittest

from stockmarket.core.data import (
    DataUnavailable,
    RateLimited,
    TwelveDataProvider,
    create_market_data_provider,
)
from stockmarket.core.data.twelvedata import _TwelveDataHTTPError
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass

KEY = "td-test-key-should-never-leak"
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def bars(*stamps):
    return {"meta": {"symbol": "SAP", "mic_code": "XETR"}, "status": "ok", "values": [
        {"datetime": s, "open": "100", "high": "101", "low": "99", "close": "100.5",
         "volume": "1200"} for s in stamps]}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, params, timeout):
        self.calls.append(dict(params))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class TwelveDataProviderTest(unittest.TestCase):
    def setUp(self):
        self.markets = default_markets()
        self.sap = self.markets.get("DE").instrument(
            "SAP", mic="XETR", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.instruments = {self.sap.instrument_id: self.sap}

    def provider(self, transport, **kwargs):
        return TwelveDataProvider(self.instruments, KEY, markets=self.markets,
                                  transport=transport, clock=lambda: NOW, **kwargs)

    def test_ohlcv_request_uses_mic_utc_ascending_and_normalizes(self):
        transport = FakeTransport(bars("2026-10-05 13:55:00", "2026-10-05 14:00:00"))
        df = self.provider(transport).get_ohlcv(
            self.sap, "5m", NOW - timedelta(minutes=5), NOW)
        params = transport.calls[0]
        self.assertEqual(params["symbol"], "SAP")
        self.assertEqual(params["mic_code"], "XETR")
        self.assertEqual(params["timezone"], "UTC")
        self.assertEqual(params["order"], "asc")
        self.assertEqual(params["interval"], "5min")
        self.assertEqual(params["start_date"], "2026-10-05 13:55:00")
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(df.index[-1], NOW)
        self.assertEqual(df["close"].dtype.kind, "f")

    def test_empty_values_return_empty_frame(self):
        df = self.provider(FakeTransport({"status": "ok", "values": []})).get_ohlcv(
            self.sap, "1d", NOW - timedelta(days=5), NOW)
        self.assertTrue(df.empty)

    def test_daily_bars_dated_before_intraday_start_are_trimmed_to_request(self):
        from stockmarket.core.data.quality import validate_bars
        start = NOW - timedelta(days=3)
        transport = FakeTransport(bars("2026-10-02", "2026-10-05"))
        df = self.provider(transport).get_ohlcv(self.sap, "1d", start, NOW)
        self.assertEqual(list(df.index), [datetime(2026, 10, 5, tzinfo=timezone.utc)])
        report = validate_bars(df, "1d", timezone=self.sap.timezone,
                               requested_start=start, requested_end=NOW)
        self.assertNotIn("OUT_OF_REQUEST_RANGE", report.issues)

    def test_error_json_with_http_200_is_mapped(self):
        cases = [(429, RateLimited), (401, DataUnavailable), (403, DataUnavailable),
                 (400, DataUnavailable), (500, ConnectionError)]
        for code, error in cases:
            with self.subTest(code=code):
                transport = FakeTransport({"status": "error", "code": code,
                                           "message": f"bad apikey {KEY}"})
                with self.assertRaises(error) as caught:
                    self.provider(transport).get_ohlcv(
                        self.sap, "1d", NOW - timedelta(days=5), NOW)
                self.assertNotIn(KEY, str(caught.exception))

    def test_http_errors_are_mapped_without_leaking_key(self):
        for code, error in [(429, RateLimited), (401, DataUnavailable), (503, ConnectionError)]:
            with self.subTest(code=code), self.assertRaises(error) as caught:
                self.provider(FakeTransport(_TwelveDataHTTPError(code))).get_ohlcv(
                    self.sap, "1d", NOW - timedelta(days=5), NOW)
            self.assertNotIn(KEY, str(caught.exception))

    def test_repr_masks_key_and_mismatched_instrument_rejected(self):
        provider = self.provider(FakeTransport(
            {"meta": {"symbol": "SIE", "mic_code": "XETR"}, "values": []}))
        self.assertNotIn(KEY, repr(provider))
        with self.assertRaises(DataUnavailable):
            provider.get_ohlcv(self.sap, "1d", NOW - timedelta(days=5), NOW)

    def test_nasdaq_requested_by_exchange_and_segment_mic_accepted(self):
        aapl = self.markets.get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.instruments[aapl.instrument_id] = aapl
        payload = bars("2026-10-05 13:55:00")
        payload["meta"] = {"symbol": "AAPL", "mic_code": "XNGS"}
        transport = FakeTransport(payload)
        df = self.provider(transport).get_ohlcv(
            aapl, "5m", NOW - timedelta(minutes=5), NOW)
        self.assertEqual(transport.calls[0]["exchange"], "NASDAQ")
        self.assertNotIn("mic_code", transport.calls[0])
        self.assertEqual(len(df), 1)
        payload = bars("2026-10-05 13:55:00")
        payload["meta"] = {"symbol": "AAPL", "mic_code": "XNYS"}
        with self.assertRaises(DataUnavailable):
            self.provider(FakeTransport(payload)).get_ohlcv(
                aapl, "5m", NOW - timedelta(minutes=5), NOW)

    def test_plan_entitlement_error_is_labelled(self):
        message = ("This symbol is available starting with the Grow or Venture plan. "
                   "Consider upgrading now")
        for response in ({"status": "error", "code": 404, "message": message},
                         _TwelveDataHTTPError(404, message)):
            with self.subTest(response=type(response).__name__), \
                    self.assertRaises(DataUnavailable) as caught:
                self.provider(FakeTransport(response)).get_ohlcv(
                    self.sap, "1d", NOW - timedelta(days=5), NOW)
            self.assertIn("plan", str(caught.exception))

    def test_non_numeric_bar_rejected(self):
        bad = bars("2026-10-05 14:00:00")
        bad["values"][0]["close"] = "n/a"
        with self.assertRaises(DataUnavailable):
            self.provider(FakeTransport(bad)).get_ohlcv(
                self.sap, "1d", NOW - timedelta(days=5), NOW)

    def test_quote_uses_latest_one_minute_bar(self):
        transport = FakeTransport(bars("2026-10-05 13:59:00", "2026-10-05 14:00:00"))
        quote = self.provider(transport).get_quote(self.sap)
        self.assertEqual(transport.calls[0]["interval"], "1min")
        self.assertEqual(quote.price, 100.5)
        self.assertEqual(quote.timestamp, NOW)
        self.assertEqual(quote.provider, "twelvedata")

    def test_exhausted_minute_budget_raises_rate_limited_without_sending(self):
        clock = [0.0]
        transport = FakeTransport(*[bars("2026-10-05 14:00:00")] * 3)
        provider = self.provider(transport, requests_per_minute=2,
                                 monotonic=lambda: clock[0])
        provider.get_quote(self.sap)
        clock[0] = 15.0
        provider.get_quote(self.sap)
        clock[0] = 20.0
        with self.assertRaises(RateLimited) as caught:
            provider.get_quote(self.sap)
        self.assertEqual(caught.exception.retry_after, 40.0)
        self.assertEqual(len(transport.calls), 2)
        clock[0] = 60.0
        provider.get_quote(self.sap)
        self.assertEqual(len(transport.calls), 3)

    def test_resilient_scan_beyond_budget_waits_outside_timeout(self):
        # Regression: the pacing wait used to run inside ResilientProvider's 10s timeout, so the
        # first request over budget failed with ProviderTimeout -> DataUnavailable.
        from stockmarket.core.data import DataPolicy, ResilientProvider
        clock = [0.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        transport = FakeTransport(*[bars("2026-10-02", "2026-10-05")] * 3)
        raw = self.provider(transport, requests_per_minute=2, monotonic=lambda: clock[0])
        wrapped = ResilientProvider(raw, policy=DataPolicy(), clock=lambda: NOW, sleep=sleep)
        try:
            for _ in range(3):
                df = wrapped.get_ohlcv(self.sap, "1d", NOW - timedelta(days=5), NOW)
                self.assertEqual(len(df), 2)
        finally:
            wrapped.close()
        self.assertEqual(len(transport.calls), 3)
        self.assertIn(60.0, slept)
        self.assertEqual(wrapped.breaker_state, "closed")

    def test_invalid_construction_rejected(self):
        with self.assertRaises(ValueError):
            TwelveDataProvider(self.instruments, "  ")
        with self.assertRaises(ValueError):
            TwelveDataProvider(self.instruments, KEY, timeout=0)

    def test_factory_requires_key_and_is_research_only(self):
        with self.assertRaisesRegex(ValueError, "TWELVEDATA_API_KEY"):
            create_market_data_provider(
                "twelvedata", self.instruments, markets=self.markets, env={})
        provider = create_market_data_provider(
            "twelvedata", self.instruments, markets=self.markets,
            env={"TWELVEDATA_API_KEY": KEY})
        self.assertIsInstance(provider, TwelveDataProvider)
        self.assertTrue(provider.research_only)


if __name__ == "__main__":
    unittest.main()
