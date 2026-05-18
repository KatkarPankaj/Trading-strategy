"""Market controller for managing market data and API calls."""

from typing import Dict, Optional, List
import pandas as pd
import streamlit as st

from ..models import MarketData, MarketQuote
from ..utils.logger import AppLogger
from ..utils.config_loader import ConfigLoader


class MarketController:
    """Controls market data operations and API interactions.
    
    Handles:
    - Fetching market quotes
    - Caching prices
    - Managing watchlists
    - Market configuration
    """
    
    def __init__(self, logger: AppLogger = None, config_dir: str = None):
        """Initialize market controller.
        
        Args:
            logger: AppLogger instance
            config_dir: Path to config directory
        """
        self.market_data = MarketData()
        self.logger = logger or AppLogger(__name__)
        self.config = ConfigLoader(config_dir).get_market_config()
        self.cache_ttl = 15  # seconds
    
    def get_market_config(self, market: str) -> Dict:
        """Get configuration for a market.
        
        Args:
            market: Market name ('NSE' or 'US')
            
        Returns:
            Market configuration dictionary
        """
        return self.config.get(market, {})
    
    def get_watchlist(self, market: str) -> List[str]:
        """Get watchlist for a market.
        
        Args:
            market: Market name
            
        Returns:
            List of symbols
        """
        market_cfg = self.get_market_config(market)
        return market_cfg.get('watchlist', [])
    
    def get_market_hours(self, market: str) -> dict:
        """Get market hours for a market.
        
        Args:
            market: Market name
            
        Returns:
            Dictionary with open/close times and timezone
        """
        market_cfg = self.get_market_config(market)
        return {
            'timezone': market_cfg.get('timezone'),
            'market_open': market_cfg.get('market_open'),
            'market_close': market_cfg.get('market_close'),
            'entry_cutoff': market_cfg.get('entry_cutoff_time'),
            'square_off': market_cfg.get('square_off_time'),
        }
    
    def get_intraday_charges(self, market: str, side: str = None) -> dict:
        """Get intraday trading charges for a market.
        
        Args:
            market: Market name
            side: Trade side ('BUY', 'SELL', etc.) - optional
            
        Returns:
            Dictionary with charge details
        """
        market_cfg = self.get_market_config(market)
        return market_cfg.get('intraday_charges', {})
    
    @st.cache_data(ttl=15, show_spinner=False)
    def _cached_fetch_nse_quote(self, symbol: str) -> Optional[Dict]:
        """Cached NSE quote fetch (streamlit cache)."""
        try:
            from ..quotes import get_default_quote_service

            nse_sym = symbol.replace(".NS", "").upper()
            self.logger.info(f"Fetching NSE quote: {nse_sym}")
            q = get_default_quote_service().get_nse_quote(symbol)
            price = float(q.price)
            pchange = float(q.pchange)

            quote = MarketQuote(
                symbol=symbol,
                price=price,
                change_pct=pchange,
            )
            self.market_data.update_quote(quote)
            self.logger.info(f"NSE {nse_sym}: Rs {price:.2f} ({pchange:+.2f}%)")

            return {
                "symbol": symbol,
                "price": price,
                "change": 0.0,
                "pchange": pchange,
            }

        except Exception as e:
            if "429" in str(e) or "Rate limit" in str(e):
                self.logger.error(f"NSE API rate limit (429) for {symbol}: {e}")
            elif "timeout" in str(e).lower():
                self.logger.warning(f"NSE API timeout for {symbol}: {e}")
            else:
                self.logger.error(f"NSE API error for {symbol}: {e}")
            return None
    
    @st.cache_data(ttl=15, show_spinner=False)
    def _cached_fetch_us_quote(self, symbol: str) -> Optional[Dict]:
        """Cached US quote fetch (streamlit cache)."""
        try:
            from ..quotes import get_default_quote_service

            self.logger.info(f"Fetching US quote: {symbol}")
            q = get_default_quote_service().get_us_quote(symbol)
            price = float(q.price)
            pchange = float(q.pchange)

            quote = MarketQuote(
                symbol=symbol,
                price=price,
                change_pct=pchange,
            )
            self.market_data.update_quote(quote)
            self.logger.info(f"US {symbol}: ${price:.2f} ({pchange:+.2f}%)")

            return {
                "symbol": symbol,
                "price": price,
                "pchange": pchange,
            }

        except Exception as e:
            if "429" in str(e) or "Rate limit" in str(e):
                self.logger.error(f"US quote provider rate limit (429) for {symbol}: {e}")
            elif "timeout" in str(e).lower() or "timed out" in str(e).lower():
                self.logger.warning(f"US quote provider timeout for {symbol}: {e}")
            elif "No data found" in str(e):
                self.logger.warning(f"No data for {symbol} (invalid ticker?)")
            else:
                self.logger.error(f"US quote provider error for {symbol}: {e}")
            return None
    
    def fetch_quote(self, symbol: str, market: str) -> Optional[Dict]:
        """Fetch quote for a symbol from specified market.
        
        Args:
            symbol: Stock symbol
            market: Market name ('NSE' or 'US')
            
        Returns:
            Quote dictionary or None
        """
        try:
            if market == "NSE":
                return self._cached_fetch_nse_quote(symbol)
            elif market == "US":
                return self._cached_fetch_us_quote(symbol)
            else:
                self.logger.error(f"Unknown market: {market}")
                return None
        
        except Exception as e:
            self.logger.error(f"Error fetching quote for {symbol}: {e}")
            return None
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get all cached prices.
        
        Returns:
            Dictionary mapping symbols to prices
        """
        return self.market_data.get_all_prices()
