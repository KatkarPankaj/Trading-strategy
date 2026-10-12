"""Provider capabilities, per-market routing, entitlement checks and coded provider errors."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from stockmarket.core.data import (
    DataPolicy,
    DataQualityError,
    DataUnavailable,
    EntitlementMissing,
    HistoryDepthUnavailable,
    MockProvider,
    ProviderAuthenticationFailed,
    RateLimited,
    ResilientProvider,
    RoutedMarketDataProvider,
    TwelveDataProvider,
    UnsupportedInterval,
    UnsupportedMarket,
    UnsupportedSymbol,
    capabilities_for,
    create_market_data_provider,
    expected_provider_name,
    is_live_eligible,
    provider_error_code,
    validate_request,
)
from stockmarket.core.data.provider import Quote
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.settings import ConfigurationError, load_settings

KEY = "td-routing-key-never-leaks"
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, params, timeout):
        self.calls.append(dict(params))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeProvider(MockProvider):
    """A MockProvider that answers under another name, to exercise multi-provider routing."""

    def __init__(self, name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.markets = default_markets()
        self.sap = self.markets.get("DE").instrument(
            "SAP", mic="XETR", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.aapl = self.markets.get("US").instrument(
            "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)
        self.instruments = {i.instrument_id: i for i in (self.sap, self.aapl)}


class CapabilityRegistryTest(Fixtures):
    def test_no_current_provider_or_unknown_name_is_live_eligible(self):
        for name in ("mock", "yahoo", "twelvedata", "alpaca", "", "MOCK"):
            with self.subTest(name=name):
                self.assertFalse(is_live_eligible(name))
        for name in ("mock", "yahoo", "twelvedata"):
            self.assertTrue(capabilities_for(name).research_only)
            self.assertFalse(capabilities_for(name).streaming)

    def test_unsupported_exchange_and_interval_rejected_before_any_call(self):
        caps = capabilities_for("twelvedata")
        otc = replace(self.aapl, exchange="OTCM")
        with self.assertRaises(UnsupportedMarket) as caught:
            validate_request(caps, otc)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_MARKET")
        with self.assertRaises(UnsupportedMarket):
            validate_request(caps, replace(self.aapl, market="JP"))
        with self.assertRaises(UnsupportedInterval) as caught:
            validate_request(caps, self.sap, interval="2h")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_INTERVAL")
        self.assertIsInstance(caught.exception, ValueError)

    def test_declared_entitlements_and_history_depth_are_enforced(self):
        with self.assertRaises(EntitlementMissing):
            validate_request(capabilities_for("twelvedata"), self.sap,
                             entitled_exchanges=frozenset({"XNAS"}))
        validate_request(capabilities_for("twelvedata"), self.aapl,
                         entitled_exchanges=frozenset({"XNAS"}))
        with self.assertRaises(HistoryDepthUnavailable) as caught:
            validate_request(capabilities_for("yahoo"), self.aapl, interval="1m",
                             start=NOW - timedelta(days=45), now=NOW)
        self.assertEqual(caught.exception.code, "HISTORY_DEPTH_UNAVAILABLE")
        validate_request(capabilities_for("yahoo"), self.aapl, interval="1d",
                         start=NOW - timedelta(days=3650), now=NOW)


class CodedErrorTest(Fixtures):
    def provider(self, *responses):
        return TwelveDataProvider(self.instruments, KEY, markets=self.markets,
                                  transport=Transport(*responses), clock=lambda: NOW)

    def test_twelvedata_errors_keep_distinct_codes(self):
        cases = [
            ({"status": "error", "code": 401, "message": f"invalid apikey {KEY}"},
             ProviderAuthenticationFailed, "AUTHENTICATION_FAILED"),
            ({"status": "error", "code": 403, "message": "forbidden"},
             EntitlementMissing, "ENTITLEMENT_MISSING"),
            ({"status": "error", "code": 400, "message": "This symbol is available starting "
                                                         "with the Pro plan, upgrade"},
             EntitlementMissing, "ENTITLEMENT_MISSING"),
            ({"status": "error", "code": 404, "message": "symbol not found"},
             UnsupportedSymbol, "UNSUPPORTED_SYMBOL"),
            ({"status": "error", "code": 429, "message": "run out of API credits"},
             RateLimited, "RATE_LIMITED"),
        ]
        for response, error, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(error) as caught:
                    self.provider(response).get_ohlcv(
                        self.sap, "1d", NOW - timedelta(days=5), NOW)
                self.assertEqual(provider_error_code(caught.exception), code)
                self.assertNotIn(KEY, str(caught.exception))

    def test_generic_codes_and_stale_classification(self):
        self.assertEqual(provider_error_code(DataQualityError(("STALE_QUOTE",))), "STALE_DATA")
        self.assertEqual(provider_error_code(DataQualityError(("NAN_PRICE",))),
                         "DATA_QUALITY_FAILED")
        self.assertEqual(provider_error_code(ConnectionError()), "PROVIDER_CONNECTION_FAILED")
        self.assertEqual(provider_error_code(RuntimeError()), "PROVIDER_ERROR")
        self.assertTrue(issubclass(UnsupportedSymbol, DataUnavailable))

    def test_resilient_wrapper_does_not_retry_auth_or_entitlement_failures(self):
        for response, error in (
                ({"status": "error", "code": 401, "message": "bad key"},
                 ProviderAuthenticationFailed),
                ({"status": "error", "code": 403, "message": "forbidden"}, EntitlementMissing)):
            with self.subTest(error=error.__name__):
                transport = Transport(response, response, response)
                raw = TwelveDataProvider(self.instruments, KEY, markets=self.markets,
                                         transport=transport, clock=lambda: NOW)
                wrapped = ResilientProvider(raw, policy=DataPolicy(), clock=lambda: NOW,
                                            sleep=lambda _: None)
                try:
                    with self.assertRaises(error):
                        wrapped.get_ohlcv(self.sap, "1d", NOW - timedelta(days=5), NOW)
                finally:
                    wrapped.close()
                self.assertEqual(len(transport.calls), 1)


class RoutingTest(Fixtures):
    def test_factory_returns_plain_provider_without_routes(self):
        provider = create_market_data_provider("mock", self.instruments, markets=self.markets,
                                               env={})
        self.assertIsInstance(provider, MockProvider)

    def test_factory_routes_market_to_its_provider(self):
        env = {"TWELVEDATA_API_KEY": KEY}
        provider = create_market_data_provider(
            "mock", self.instruments, markets=self.markets, env=env, routes={"de": "twelvedata"})
        self.assertIsInstance(provider, RoutedMarketDataProvider)
        self.assertEqual(provider.routes, {"DE": "twelvedata"})
        self.assertEqual(provider.provider_name_for(self.sap), "twelvedata")
        self.assertEqual(provider.provider_name_for(self.aapl), "mock")
        self.assertTrue(provider.research_only)
        self.assertNotIn(KEY, provider.name)

    def test_declared_entitlement_blocks_call_before_network(self):
        env = {"TWELVEDATA_API_KEY": KEY, "TWELVEDATA_ENTITLED_EXCHANGES": "XNAS,XNYS"}
        provider = create_market_data_provider(
            "twelvedata", self.instruments, markets=self.markets, env=env)
        self.assertIsInstance(provider, RoutedMarketDataProvider)
        self.assertEqual(provider.name, "twelvedata")
        with self.assertRaises(EntitlementMissing):
            provider.get_quote(self.sap)

    def test_routed_quotes_pass_route_aware_identity_checks(self):
        us = MockProvider(self.instruments, clock=lambda: NOW)
        de = FakeProvider("de_feed", self.instruments, clock=lambda: NOW)
        us.set_quote(self.aapl.instrument_id, 190.0, NOW)
        de.set_quote(self.sap.instrument_id, 120.0, NOW)
        router = RoutedMarketDataProvider(us, {"DE": de}, clock=lambda: NOW)
        wrapped = ResilientProvider(router, clock=lambda: NOW)
        try:
            self.assertEqual(wrapped.get_quote(self.sap).provider, "de_feed")
            self.assertEqual(wrapped.get_quote(self.aapl).provider, "mock")
            self.assertEqual(expected_provider_name(wrapped, self.sap), "de_feed")
            # A quote attributed to the wrong leg is still rejected.
            de._quotes[self.sap.instrument_id] = Quote(self.sap.instrument_id, 120.0, NOW, "mock")
            with self.assertRaises(DataQualityError) as caught:
                wrapped.get_quote(self.sap)
            self.assertEqual(caught.exception.issues, ("PROVIDER_MISMATCH",))
        finally:
            wrapped.close()


class RouteSettingsTest(unittest.TestCase):
    def test_routes_are_parsed_and_validated(self):
        settings = load_settings({"MARKETS": "US,DE", "DATA_PROVIDER": "mock",
                                  "DATA_PROVIDER_DE": "TwelveData"})
        self.assertEqual(settings.data_provider_routes, (("DE", "twelvedata"),))
        self.assertEqual(settings.to_public_dict()["data_provider_routes"], {"DE": "twelvedata"})
        self.assertEqual(load_settings({}).data_provider_routes, ())

    def test_bad_routes_and_unknown_providers_fail_fast(self):
        for env, message in (
                ({"MARKETS": "US", "DATA_PROVIDER_DE": "mock"}, "not listed in MARKETS"),
                ({"MARKETS": "US,DE", "DATA_PROVIDER_DE": "bloomberg"}, "not a supported"),
                ({"DATA_PROVIDER": "polygon"}, "not a supported provider")):
            with self.subTest(env=env):
                with self.assertRaises(ConfigurationError) as caught:
                    load_settings(env)
                self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
