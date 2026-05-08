"""Persistence layer for abstracted data storage."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime


class StorageBackend(ABC):
    """Abstract base class for storage backends.
    
    Implementations can use JSON files, SQLite, PostgreSQL, etc.
    """
    
    @abstractmethod
    def initialize(self) -> None:
        """Initialize storage backend (create tables, files, etc.)."""
        pass
    
    @abstractmethod
    def save_portfolio_state(self, portfolio_id: str, state_data: Dict[str, Any]) -> None:
        """Save portfolio state.
        
        Args:
            portfolio_id: Unique portfolio identifier
            state_data: Portfolio state dictionary
        """
        pass
    
    @abstractmethod
    def load_portfolio_state(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        """Load portfolio state.
        
        Args:
            portfolio_id: Unique portfolio identifier
            
        Returns:
            Portfolio state dictionary or None if not found
        """
        pass
    
    @abstractmethod
    def save_trade(self, portfolio_id: str, trade_data: Dict[str, Any]) -> str:
        """Save a trade record.
        
        Args:
            portfolio_id: Unique portfolio identifier
            trade_data: Trade information
            
        Returns:
            Trade ID
        """
        pass
    
    @abstractmethod
    def load_trades(self, portfolio_id: str, limit: int = None) -> List[Dict[str, Any]]:
        """Load trades for a portfolio.
        
        Args:
            portfolio_id: Unique portfolio identifier
            limit: Maximum number of trades to return
            
        Returns:
            List of trade dictionaries
        """
        pass
    
    @abstractmethod
    def save_position(self, portfolio_id: str, position_data: Dict[str, Any]) -> None:
        """Save position information.
        
        Args:
            portfolio_id: Unique portfolio identifier
            position_data: Position information
        """
        pass
    
    @abstractmethod
    def load_positions(self, portfolio_id: str) -> List[Dict[str, Any]]:
        """Load positions for a portfolio.
        
        Args:
            portfolio_id: Unique portfolio identifier
            
        Returns:
            List of position dictionaries
        """
        pass
    
    @abstractmethod
    def delete_portfolio(self, portfolio_id: str) -> None:
        """Delete a portfolio and all its data.
        
        Args:
            portfolio_id: Unique portfolio identifier
        """
        pass
    
    @abstractmethod
    def get_portfolio_list(self) -> List[Dict[str, Any]]:
        """Get list of all portfolios.
        
        Returns:
            List of portfolio metadata dictionaries
        """
        pass
