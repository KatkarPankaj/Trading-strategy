"""Trade data model and related classes."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class TradeType(Enum):
    """Enumeration of trade types."""
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(Enum):
    """Enumeration of trade statuses."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PENDING = "PENDING"


@dataclass
class Trade:
    """Represents a single trade transaction.
    
    Attributes:
        symbol: Stock symbol being traded
        trade_type: Type of trade (LONG or SHORT)
        entry_price: Price at which position was entered
        entry_time: Timestamp of entry
        qty: Quantity of shares
        entry_charges: Commission/charges paid at entry
        exit_price: Price at which position was exited (if closed)
        exit_time: Timestamp of exit (if closed)
        exit_charges: Commission/charges paid at exit
        pnl: Profit/Loss from the trade
        pnl_pct: Profit/Loss percentage
        status: Current status of the trade
    """
    symbol: str
    trade_type: TradeType
    entry_price: float
    entry_time: datetime
    qty: int
    entry_charges: float = 0.0
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_charges: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    status: TradeStatus = TradeStatus.PENDING
    
    def close(self, exit_price: float, exit_time: datetime, exit_charges: float = 0.0):
        """Close an open trade.
        
        Args:
            exit_price: Price at exit
            exit_time: Time of exit
            exit_charges: Charges at exit
        """
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.exit_charges = exit_charges
        self.status = TradeStatus.CLOSED
        
        # Calculate PnL
        if self.trade_type == TradeType.LONG:
            gross_pnl = (exit_price - self.entry_price) * self.qty
        else:  # SHORT
            gross_pnl = (self.entry_price - exit_price) * self.qty
        
        total_charges = self.entry_charges + exit_charges
        self.pnl = gross_pnl - total_charges
        self.pnl_pct = (self.pnl / (self.entry_price * self.qty)) * 100 if self.entry_price > 0 else 0
    
    def get_current_pnl(self, current_price: float) -> dict:
        """Calculate current P&L for an open trade.
        
        Args:
            current_price: Current market price
            
        Returns:
            Dictionary with 'pnl' and 'pnl_pct' keys
        """
        if self.status == TradeStatus.CLOSED:
            return {"pnl": self.pnl, "pnl_pct": self.pnl_pct}
        
        if self.trade_type == TradeType.LONG:
            gross_pnl = (current_price - self.entry_price) * self.qty
        else:  # SHORT
            gross_pnl = (self.entry_price - current_price) * self.qty
        
        current_pnl = gross_pnl - self.entry_charges
        current_pnl_pct = (current_pnl / (self.entry_price * self.qty)) * 100 if self.entry_price > 0 else 0
        
        return {"pnl": current_pnl, "pnl_pct": current_pnl_pct}


@dataclass
class TradePosition:
    """Represents a current open position in a symbol.
    
    Attributes:
        symbol: Stock symbol
        trade_type: Type of position (LONG or SHORT)
        quantity: Current quantity held
        avg_price: Average entry price
        entry_charges: Total charges paid
        trades: List of Trade objects that make up this position
    """
    symbol: str
    trade_type: TradeType
    quantity: int
    avg_price: float
    entry_charges: float
    trades: list = field(default_factory=list)
    
    def add_trade(self, trade: Trade):
        """Add a trade to this position (for averaging).
        
        Args:
            trade: Trade object to add
        """
        total_qty = self.quantity + trade.qty
        self.avg_price = (
            (self.avg_price * self.quantity + trade.entry_price * trade.qty) / total_qty
            if total_qty > 0 else self.avg_price
        )
        self.quantity = total_qty
        self.entry_charges += trade.entry_charges
        self.trades.append(trade)
    
    def get_value_at_price(self, price: float) -> float:
        """Get position value at a specific price.
        
        Args:
            price: Current market price
            
        Returns:
            Position value in currency
        """
        return self.quantity * price
