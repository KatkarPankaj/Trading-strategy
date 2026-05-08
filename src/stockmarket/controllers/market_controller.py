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
        """Cached NSE quote fetch (streamlit cache).
        
        Args:
            symbol: Stock symbol
            
        Returns:
            Quote dictionary or None
        """
        try:
            from nsepython import nsefetch
            
            # Convert to NSE symbol format if needed
            nse_symbol = symbol.replace('.NS', '').upper()
            
            self.logger.info(f"Fetching NSE quote: {nse_symbol}")
            quote_data = nsefetch(nse_symbol)
            
            if not quote_data:
                self.logger.warning(f"No data for NSE {nse_symbol}")
                return None
            
            price = float(quote_data.get('price', 0))
            change = float(quote_data.get('change', 0))
            pchange = float(quote_data.get('pchange', 0))
            
            quote = MarketQuote(
                symbol=symbol,
                price=price,
                change_pct=pchange
            )
            
            self.market_data.update_quote(quote)
            self.logger.info(f"NSE {nse_symbol}: Rs {price:.2f} ({pchange:+.2f}%)")
            
            return {
                'symbol': symbol,
                'price': price,
                'change': change,
                'pchange': pchange,
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
        """Cached US quote fetch (streamlit cache).
        
        Args:
            symbol: Stock symbol
            
        Returns:
            Quote dictionary or None
        """
        try:
            import yfinance as yf
            
            self.logger.info(f"Fetching US quote: {symbol}")
            ticker = yf.Ticker(symbol.upper())
            
            # Get intraday data
            intraday = ticker.history(period="1d", interval="1m", prepost=False, auto_adjust=False)
            
            if intraday is None or intraday.empty:
                intraday = ticker.history(period="5d", interval="5m", prepost=False, auto_adjust=False)
            
            if intraday is None or intraday.empty:
                self.logger.warning(f"No data for US {symbol}")
                return None
            
            intraday = intraday.dropna(subset=["Close"]).copy()
            if intraday.empty:
                return None
            
            latest = intraday.iloc[-1]
            price = float(latest.get("Close") or 0.0)
            
            # Get daily data for change %
            daily_hist = ticker.history(period="2d", interval="1d", auto_adjust=False)
            prev_close = 0.0
            if daily_hist is not None and not daily_hist.empty:
                daily_hist = daily_hist.dropna(subset=["Close"])
                if len(daily_hist) >= 2:
                    prev_close = float(daily_hist.iloc[-2]["Close"] or 0.0)
                elif len(daily_hist) == 1:
                    prev_close = float(daily_hist.iloc[-1]["Close"] or 0.0)
            
            pchange = ((price - prev_close) / max(prev_close, 1e-6)) * 100 if prev_close > 0 else 0.0
            
            quote = MarketQuote(
                symbol=symbol,
                price=price,
                change_pct=pchange
            )
            
            self.market_data.update_quote(quote)
            self.logger.info(f"US {symbol}: ${price:.2f} ({pchange:+.2f}%)")
            
            return {
                'symbol': symbol,
                'price': price,
                'pchange': pchange,
            }
        
        except Exception as e:
            if "429" in str(e) or "Rate limit" in str(e):
                self.logger.error(f"yfinance rate limit (429) for {symbol}: {e}")
            elif "timeout" in str(e).lower() or "timed out" in str(e).lower():
                self.logger.warning(f"yfinance timeout for {symbol}: {e}")
            elif "No data found" in str(e):
                self.logger.warning(f"No data for {symbol} (invalid ticker?)")
            else:
                self.logger.error(f"yfinance error for {symbol}: {e}")
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
