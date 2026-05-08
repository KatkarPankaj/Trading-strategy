"""Market data model for price information and market operations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


@dataclass
class MarketQuote:
    """Represents a market quote for a symbol.
    
    Attributes:
        symbol: Stock symbol
        price: Current price
        high: Day high
        low: Day low
        volume: Trading volume
        change_pct: Percentage change
        vwap: Volume weighted average price
        timestamp: When the quote was fetched
    """
    symbol: str
    price: float
    high: float = 0.0
    low: float = 0.0
    volume: int = 0
    change_pct: float = 0.0
    vwap: float = 0.0
    timestamp: datetime = None
    
    def __post_init__(self):
        """Initialize timestamp if not set."""
        if self.timestamp is None:
            self.timestamp = datetime.now()


class MarketData:
    """Manages market data and quote information.
    
    Provides:
    - Cache for market quotes
    - Price lookups
    - Market statistics
    """
    
    def __init__(self):
        """Initialize market data manager."""
        self._quotes: Dict[str, MarketQuote] = {}
    
    def update_quote(self, quote: MarketQuote) -> None:
        """Update or add a market quote.
        
        Args:
            quote: MarketQuote object
        """
        self._quotes[quote.symbol] = quote
    
    def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        """Get market quote for a symbol.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            MarketQuote if available, None otherwise
        """
        return self._quotes.get(symbol)
    
    def get_price(self, symbol: str) -> float:
        """Get current price for a symbol.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            Current price or 0.0 if not available
        """
        quote = self._quotes.get(symbol)
        return quote.price if quote else 0.0
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get all available prices.
        
        Returns:
            Dictionary mapping symbols to prices
        """
        return {symbol: quote.price for symbol, quote in self._quotes.items()}
    
    def has_symbol(self, symbol: str) -> bool:
        """Check if symbol has cached quote.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            True if quote is available
        """
        return symbol in self._quotes
    
    def clear(self) -> None:
        """Clear all cached quotes."""
        self._quotes.clear()
