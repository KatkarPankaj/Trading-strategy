import unittest

from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass


class MarketInstrumentFactoryTests(unittest.TestCase):
    def setUp(self):
        self.markets = default_markets()

    def test_same_instrument_model_captures_us_and_india_market_facts(self):
        us = self.markets.get("US").instrument(
            "AAPL",
            mic="XNAS",
            asset_class=AssetClass.EQUITY,
            tick_size=0.01,
            shortable=True,
        )
        india = self.markets.get("IN").instrument(
            "RELIANCE",
            mic="XNSE",
            asset_class=AssetClass.EQUITY,
            tick_size=0.05,
        )

        self.assertEqual((us.currency, us.timezone), ("USD", "America/New_York"))
        self.assertEqual((india.currency, india.timezone), ("INR", "Asia/Kolkata"))
        self.assertTrue(us.is_valid_price(100.01))
        self.assertTrue(india.is_valid_price(100.05))

    def test_explicit_invalid_lot_size_is_not_replaced_by_market_default(self):
        with self.assertRaisesRegex(ValueError, "lot_size"):
            self.markets.get("US").instrument(
                "AAPL",
                mic="XNAS",
                asset_class=AssetClass.EQUITY,
                tick_size=0.01,
                lot_size=0,
            )

    def test_rejects_exchange_outside_market_definition(self):
        with self.assertRaisesRegex(ValueError, "not an exchange"):
            self.markets.get("US").instrument(
                "RELIANCE",
                mic="XNSE",
                asset_class=AssetClass.EQUITY,
                tick_size=0.05,
            )


if __name__ == "__main__":
    unittest.main()
