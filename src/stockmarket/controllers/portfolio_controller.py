"""Portfolio controller for managing portfolio operations."""

from typing import Dict, Optional
import pandas as pd

from ..models import Portfolio, Trade, TradeType
from ..persistence.factory import StorageFactory
from ..utils.logger import AppLogger


class PortfolioController:
    """Controls portfolio operations and state management.
    
    Coordinates between Portfolio model and storage backend.
    Handles portfolio creation, loading, state updates, and reports.
    """
    
    def __init__(self, portfolio_id: str, initial_capital: float, 
                 storage_type: str = None, logger: AppLogger = None):
        """Initialize portfolio controller.
        
        Args:
            portfolio_id: Unique identifier for portfolio
            initial_capital: Starting capital for portfolio
            storage_type: Type of storage ('file', 'sqlite', etc.)
            logger: AppLogger instance for logging
        """
        self.portfolio_id = portfolio_id
        self.portfolio = Portfolio(initial_capital)
        self.storage = StorageFactory.create_storage(storage_type)
        self.storage.initialize()
        self.logger = logger or AppLogger(__name__)
    
    def load_from_storage(self) -> bool:
        """Load portfolio state from storage.
        
        Returns:
            True if successfully loaded, False if not found
        """
        state_data = self.storage.load_portfolio_state(self.portfolio_id)
        
        if not state_data:
            self.logger.warning(f"No saved state found for portfolio {self.portfolio_id}")
            return False
        
        try:
            # Reconstruct portfolio from saved state
            self.portfolio.state.capital = state_data.get('capital', self.portfolio.state.capital)
            self.portfolio.state.current_capital = state_data.get('current_capital', 
                                                                 self.portfolio.state.capital)
            self.portfolio.state.total_pnl = state_data.get('total_pnl', 0.0)
            
            self.logger.info(f"Loaded portfolio {self.portfolio_id} from storage")
            return True
        
        except Exception as e:
            self.logger.error(f"Error loading portfolio: {e}")
            return False
    
    def save_to_storage(self) -> None:
        """Save portfolio state to storage."""
        state_data = {
            'capital': self.portfolio.state.capital,
            'current_capital': self.portfolio.state.current_capital,
            'total_pnl': self.portfolio.state.total_pnl,
            'num_positions': len(self.portfolio.state.open_positions),
            'num_trades': len(self.portfolio.state.closed_trades),
        }
        
        self.storage.save_portfolio_state(self.portfolio_id, state_data)
        self.logger.debug(f"Saved portfolio {self.portfolio_id} to storage")
    
    def open_trade(self, symbol: str, trade_type: str, qty: int, 
                  entry_price: float, entry_charges: float = 0.0) -> Trade:
        """Open a new trade.
        
        Args:
            symbol: Stock symbol
            trade_type: 'LONG' or 'SHORT'
            qty: Quantity to trade
            entry_price: Entry price
            entry_charges: Commission and charges
            
        Returns:
            Trade object
            
        Raises:
            ValueError: If trade cannot be executed
        """
        trade_type_enum = TradeType(trade_type)
        trade = Trade(
            symbol=symbol,
            trade_type=trade_type_enum,
            entry_price=entry_price,
            entry_time=pd.Timestamp.now(),
            qty=qty,
            entry_charges=entry_charges
        )
        
        try:
            self.portfolio.open_position(trade)
            self.storage.save_trade(self.portfolio_id, {
                'symbol': symbol,
                'trade_type': trade_type,
                'qty': qty,
                'entry_price': entry_price,
                'entry_charges': entry_charges,
                'entry_time': str(trade.entry_time),
                'status': 'OPEN',
                'timestamp': str(pd.Timestamp.now())
            })
            self.logger.info(f"Opened {trade_type} position: {symbol} x{qty} @ {entry_price}")
            return trade
        
        except Exception as e:
            self.logger.error(f"Error opening trade: {e}")
            raise
    
    def close_trade(self, symbol: str, trade_type: str, exit_price: float,
                   exit_charges: float = 0.0) -> Optional[Trade]:
        """Close an open trade position.
        
        Args:
            symbol: Stock symbol
            trade_type: 'LONG' or 'SHORT'
            exit_price: Price to exit at
            exit_charges: Commission and charges
            
        Returns:
            Closed Trade object or None if failed
        """
        try:
            trade_type_enum = TradeType(trade_type)
            trade = self.portfolio.close_position(symbol, trade_type_enum, exit_price, exit_charges)
            
            self.storage.save_trade(self.portfolio_id, {
                'symbol': symbol,
                'trade_type': trade_type,
                'qty': trade.qty,
                'entry_price': trade.entry_price,
                'exit_price': exit_price,
                'entry_charges': trade.entry_charges,
                'exit_charges': exit_charges,
                'exit_time': str(pd.Timestamp.now()),
                'pnl': trade.pnl,
                'pnl_pct': trade.pnl_pct,
                'status': 'CLOSED',
                'timestamp': str(pd.Timestamp.now())
            })
            
            self.logger.info(f"Closed {trade_type} position: {symbol} x{trade.qty} @ {exit_price} (PnL: {trade.pnl:.2f})")
            return trade
        
        except Exception as e:
            self.logger.error(f"Error closing trade: {e}")
            return None
    
    def get_portfolio_summary(self, current_prices: Dict[str, float] = None) -> dict:
        """Get portfolio summary with metrics.
        
        Args:
            current_prices: Dictionary of current prices
            
        Returns:
            Portfolio summary dictionary
        """
        return self.portfolio.get_summary(current_prices)
    
    def get_positions_dataframe(self, current_prices: Dict[str, float]) -> pd.DataFrame:
        """Get current positions as DataFrame.
        
        Args:
            current_prices: Dictionary of current prices
            
        Returns:
            DataFrame with position details
        """
        return self.portfolio.get_portfolio_dataframe(current_prices)
    
    def get_trade_history(self, limit: int = None) -> list:
        """Get trade history from storage.
        
        Args:
            limit: Maximum number of trades to return
            
        Returns:
            List of trade dictionaries
        """
        return self.storage.load_trades(self.portfolio_id, limit)
