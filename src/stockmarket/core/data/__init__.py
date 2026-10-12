"""Market data providers and the reliability layer around them."""

from .mock import MockProvider
from .nse_sector_indices import (
    NSESectorDataUnavailable,
    NSESectorIndexObservationProvider,
)
from .factory import create_market_data_provider
from .capabilities import (
    PROVIDER_CAPABILITIES,
    ProviderCapabilities,
    capabilities_for,
    is_known_provider,
    is_live_eligible,
    validate_request,
)
from .provider import (
    INTERVALS,
    DataProviderError,
    DataQualityError,
    DataUnavailable,
    EntitlementMissing,
    HistoryDepthUnavailable,
    MarketDataProvider,
    ProviderAuthenticationFailed,
    ProviderTimeout,
    Quote,
    RateLimited,
    UnsupportedInterval,
    UnsupportedMarket,
    UnsupportedSymbol,
    expected_provider_name,
    provider_error_code,
)
from .quality import DataQualityReport, validate_bars, validate_quote
from .resilient import DataPolicy, ResilientProvider, quote_source
from .routing import RoutedMarketDataProvider
from .twelvedata import TwelveDataProvider
from .yahoo import YahooProvider, yahoo_symbol

__all__ = [
    "INTERVALS",
    "PROVIDER_CAPABILITIES",
    "DataPolicy",
    "DataProviderError",
    "DataQualityError",
    "DataQualityReport",
    "DataUnavailable",
    "EntitlementMissing",
    "HistoryDepthUnavailable",
    "MarketDataProvider",
    "MockProvider",
    "NSESectorDataUnavailable",
    "NSESectorIndexObservationProvider",
    "ProviderAuthenticationFailed",
    "ProviderCapabilities",
    "RoutedMarketDataProvider",
    "UnsupportedInterval",
    "UnsupportedMarket",
    "UnsupportedSymbol",
    "capabilities_for",
    "create_market_data_provider",
    "expected_provider_name",
    "is_known_provider",
    "is_live_eligible",
    "provider_error_code",
    "validate_request",
    "ProviderTimeout",
    "Quote",
    "RateLimited",
    "ResilientProvider",
    "TwelveDataProvider",
    "YahooProvider",
    "quote_source",
    "validate_bars",
    "validate_quote",
    "yahoo_symbol",
]
