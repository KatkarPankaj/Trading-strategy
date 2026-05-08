"""Portfolio data model for managing trading positions and state."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List
import pandas as pd

from .trade import Trade, TradePosition, TradeType


@dataclass
class PortfolioState:
    """Represents the complete state of a trading portfolio.
    
    Attributes:
        capital: Initial capital for trading
        current_capital: Remaining available capital
        open_positions: Dictionary of current open positions
        closed_trades: List of closed trades
        total_pnl: Cumulative profit/loss
        last_updated: Timestamp of last update
    """
    capital: float
    current_capital: float = field(default=0.0)
    open_positions: Dict[str, TradePosition] = field(default_factory=dict)
    closed_trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Initialize current capital if not set."""
        if self.current_capital == 0.0:
            self.current_capital = self.capital


class Portfolio:
    """Manages trading portfolio and positions.
    
    Provides methods to:
    - Open and close trades
    - Track positions
    - Calculate P&L
    - Generate portfolio reports
    """
    
    def __init__(self, initial_capital: float):
        """Initialize portfolio with starting capital.
        
        Args:
            initial_capital: Starting capital for trading
        """
        self.state = PortfolioState(
            capital=initial_capital,
            current_capital=initial_capital
        )
    
    def open_position(self, trade: Trade) -> None:
        """Open a new trade position or add to existing.
        
        Args:
            trade: Trade object to open
            
        Raises:
            ValueError: If insufficient capital
        """
        cost = trade.entry_price * trade.qty + trade.entry_charges
        
        if self.state.current_capital < cost:
            raise ValueError(
                f"Insufficient capital. Required: {cost}, Available: {self.state.current_capital}"
            )
        
        self.state.current_capital -= cost
        
        # Create or update position
        key = (trade.symbol, trade.trade_type.value)
        if key in self.state.open_positions:
            self.state.open_positions[key].add_trade(trade)
        else:
            position = TradePosition(
                symbol=trade.symbol,
                trade_type=trade.trade_type,
                quantity=trade.qty,
                avg_price=trade.entry_price,
                entry_charges=trade.entry_charges,
                trades=[trade]
            )
            self.state.open_positions[key] = position
        
        self.state.last_updated = datetime.now()
    
    def close_position(self, symbol: str, trade_type: TradeType, 
                      exit_price: float, exit_charges: float = 0.0) -> Trade:
        """Close an open position.
        
        Args:
            symbol: Stock symbol
            trade_type: Type of trade (LONG or SHORT)
            exit_price: Price at exit
            exit_charges: Charges at exit
            
        Returns:
            Closed Trade object
            
        Raises:
            ValueError: If position doesn't exist
        """
        key = (symbol, trade_type.value)
        
        if key not in self.state.open_positions:
            raise ValueError(f"No open position found for {symbol}")
        
        position = self.state.open_positions[key]
        exit_time = datetime.now()
        
        # Create consolidated trade for closing
        trade = Trade(
            symbol=symbol,
            trade_type=trade_type,
            entry_price=position.avg_price,
            entry_time=position.trades[0].entry_time,
            qty=position.quantity,
            entry_charges=position.entry_charges,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_charges=exit_charges
        )
        
        trade.close(exit_price, exit_time, exit_charges)
        
        # Update capital and P&L
        proceeds = trade.exit_price * position.quantity - exit_charges
        self.state.current_capital += proceeds
        self.state.total_pnl += trade.pnl
        
        # Move to closed trades
        self.state.closed_trades.append(trade)
        del self.state.open_positions[key]
        
        self.state.last_updated = datetime.now()
        
        return trade
    
    def get_portfolio_dataframe(self, current_prices: Dict[str, float]) -> pd.DataFrame:
        """Get current portfolio as DataFrame.
        
        Args:
            current_prices: Dictionary mapping symbols to current prices
            
        Returns:
            DataFrame with position details
        """
        if not self.state.open_positions:
            return pd.DataFrame()
        
        rows = []
        for (symbol, trade_type), position in self.state.open_positions.items():
            current_price = current_prices.get(symbol, 0.0)
            pnl_info = self._calculate_position_pnl(position, current_price)
            
            rows.append({
                'Symbol': symbol,
                'Type': trade_type,
                'Qty': position.quantity,
                'Avg Price': position.avg_price,
                'Current Price': current_price,
                'Value': position.get_value_at_price(current_price),
                'PnL': pnl_info['pnl'],
                'PnL %': pnl_info['pnl_pct'],
            })
        
        return pd.DataFrame(rows)
    
    def _calculate_position_pnl(self, position: TradePosition, current_price: float) -> dict:
        """Calculate P&L for a position.
        
        Args:
            position: TradePosition to analyze
            current_price: Current market price
            
        Returns:
            Dictionary with 'pnl' and 'pnl_pct'
        """
        if position.trade_type == TradeType.LONG:
            gross_pnl = (current_price - position.avg_price) * position.quantity
        else:  # SHORT
            gross_pnl = (position.avg_price - current_price) * position.quantity
        
        pnl = gross_pnl - position.entry_charges
        pnl_pct = (pnl / (position.avg_price * position.quantity)) * 100 if position.avg_price > 0 else 0
        
        return {"pnl": pnl, "pnl_pct": pnl_pct}
    
    def get_summary(self, current_prices: Dict[str, float] = None) -> dict:
        """Get portfolio summary.
        
        Args:
            current_prices: Dictionary mapping symbols to current prices
            
        Returns:
            Dictionary with portfolio metrics
        """
        if current_prices is None:
            current_prices = {}
        
        total_position_value = 0.0
        total_unrealized_pnl = 0.0
        
        for (symbol, trade_type), position in self.state.open_positions.items():
            current_price = current_prices.get(symbol, 0.0)
            position_value = position.get_value_at_price(current_price)
            pnl_info = self._calculate_position_pnl(position, current_price)
            
            total_position_value += position_value
            total_unrealized_pnl += pnl_info['pnl']
        
        total_value = self.state.current_capital + total_position_value
        total_pnl = self.state.total_pnl + total_unrealized_pnl
        
        return {
            "starting_capital": self.state.capital,
            "current_capital": self.state.current_capital,
            "position_value": total_position_value,
            "total_value": total_value,
            "realized_pnl": self.state.total_pnl,
            "unrealized_pnl": total_unrealized_pnl,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / self.state.capital) * 100 if self.state.capital > 0 else 0,
            "open_positions": len(self.state.open_positions),
            "closed_trades": len(self.state.closed_trades),
        }
