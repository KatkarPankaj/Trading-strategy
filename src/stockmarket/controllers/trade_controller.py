"""Trade controller for trade execution and management."""

from typing import Dict, Optional, Tuple
from datetime import datetime
import pandas as pd

from ..models import Trade, TradeType
from ..utils.logger import AppLogger


class TradeController:
    """Controls trade execution and validation.
    
    Handles:
    - Trade validation (capital, risk, limits)
    - Charge calculations
    - Trade execution
    - Trade records management
    """
    
    def __init__(self, logger: AppLogger = None):
        """Initialize trade controller.
        
        Args:
            logger: AppLogger instance
        """
        self.logger = logger or AppLogger(__name__)
    
    def validate_trade(self, symbol: str, qty: int, price: float, 
                      available_capital: float, market: str = "NSE") -> Tuple[bool, Optional[str]]:
        """Validate trade before execution.
        
        Args:
            symbol: Stock symbol
            qty: Quantity
            price: Price per unit
            available_capital: Available capital
            market: Market name
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if qty <= 0:
            return False, "Quantity must be positive"
        
        if price <= 0:
            return False, "Price must be positive"
        
        required_capital = qty * price
        if required_capital > available_capital:
            return False, f"Insufficient capital. Required: {required_capital:.2f}, Available: {available_capital:.2f}"
        
        return True, None
    
    def calculate_charges(self, market: str, qty: int, price: float, 
                         side: str = "BUY", intraday_charges: Dict = None) -> dict:
        """Calculate trading charges for a trade.
        
        Args:
            market: Market name ('NSE' or 'US')
            qty: Quantity
            price: Price per unit
            side: Trade side
            intraday_charges: Charges configuration dict
            
        Returns:
            Dictionary with charge breakdowns
        """
        if intraday_charges is None:
            intraday_charges = {}
        
        turnover = qty * price
        
        if market == "NSE":
            # NSE charges
            commission_pct = intraday_charges.get('commission_pct', 0.03) / 100
            gst_pct = intraday_charges.get('gst_pct', 18)
            stt_pct = intraday_charges.get('stt_pct', 0.025) / 100
            exchange_turnover_pct = intraday_charges.get('exchange_turnover_pct', 0.00325) / 100
            clearing_house_pct = intraday_charges.get('clearing_house_pct', 0.0002) / 100
            sebi_turnover_pct = intraday_charges.get('sebi_turnover_pct', 0.000001) / 100
            
            commission = turnover * commission_pct
            gst = commission * (gst_pct / 100)
            stt = turnover * stt_pct
            exchange = turnover * exchange_turnover_pct
            clearing = turnover * clearing_house_pct
            sebi = turnover * sebi_turnover_pct
            
            total_charges = commission + gst + stt + exchange + clearing + sebi
            
            return {
                'commission': commission,
                'gst': gst,
                'stt': stt,
                'exchange': exchange,
                'clearing': clearing,
                'sebi': sebi,
                'total': total_charges,
                'percentage': (total_charges / turnover) * 100 if turnover > 0 else 0,
            }
        
        elif market == "US":
            # US charges
            commission_pct = intraday_charges.get('commission_pct', 0.01) / 100
            slippage_pct = intraday_charges.get('slippage_pct', 0.05) / 100
            
            commission = turnover * commission_pct
            slippage = turnover * slippage_pct
            
            total_charges = commission + slippage
            
            return {
                'commission': commission,
                'slippage': slippage,
                'total': total_charges,
                'percentage': (total_charges / turnover) * 100 if turnover > 0 else 0,
            }
        
        else:
            return {'total': 0, 'percentage': 0}
    
    def calculate_pnl(self, entry_price: float, exit_price: float, qty: int,
                     trade_type: str, entry_charges: float = 0.0, 
                     exit_charges: float = 0.0) -> dict:
        """Calculate PnL for a closed trade.
        
        Args:
            entry_price: Entry price
            exit_price: Exit price
            qty: Quantity
            trade_type: 'LONG' or 'SHORT'
            entry_charges: Charges at entry
            exit_charges: Charges at exit
            
        Returns:
            Dictionary with PnL details
        """
        if trade_type == "LONG":
            gross_pnl = (exit_price - entry_price) * qty
        else:  # SHORT
            gross_pnl = (entry_price - exit_price) * qty
        
        total_charges = entry_charges + exit_charges
        net_pnl = gross_pnl - total_charges
        
        position_value = entry_price * qty
        pnl_pct = (net_pnl / position_value) * 100 if position_value > 0 else 0
        
        return {
            'gross_pnl': gross_pnl,
            'charges': total_charges,
            'net_pnl': net_pnl,
            'pnl_pct': pnl_pct,
            'roi': (net_pnl / position_value) * 100 if position_value > 0 else 0,
        }
    
    def create_trade_record(self, symbol: str, trade_type: str, qty: int,
                           entry_price: float, entry_charges: float = 0.0,
                           exit_price: float = None, exit_charges: float = 0.0) -> Trade:
        """Create a trade record.
        
        Args:
            symbol: Stock symbol
            trade_type: 'LONG' or 'SHORT'
            qty: Quantity
            entry_price: Entry price
            entry_charges: Charges at entry
            exit_price: Exit price (if closing)
            exit_charges: Charges at exit
            
        Returns:
            Trade object
        """
        trade = Trade(
            symbol=symbol,
            trade_type=TradeType(trade_type),
            entry_price=entry_price,
            entry_time=datetime.now(),
            qty=qty,
            entry_charges=entry_charges,
        )
        
        if exit_price is not None:
            trade.close(exit_price, datetime.now(), exit_charges)
        
        return trade
