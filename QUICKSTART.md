# Quick Start Guide - New Architecture

This guide shows how to use the refactored MVC architecture with practical code examples.

## 1. Basic Setup

```python
from stockmarket.controllers import PortfolioController, MarketController
from stockmarket.utils import AppLogger, ConfigLoader
from stockmarket.persistence.factory import StorageFactory

# Initialize components
logger = AppLogger("MyTradingApp")
config_loader = ConfigLoader()
storage = StorageFactory.create_storage()
storage.initialize()

# Load configurations
market_config = config_loader.get_market_config()
trading_config = config_loader.get_trading_config()

logger.info("Application initialized")
```

## 2. Create and Manage Portfolio

```python
# Create portfolio controller
portfolio_ctrl = PortfolioController(
    portfolio_id="user_1",
    initial_capital=200000,
    storage_type="sqlite",
    logger=logger
)

# Load existing portfolio or start fresh
if not portfolio_ctrl.load_from_storage():
    logger.info("Starting new portfolio")

# Get current portfolio summary
summary = portfolio_ctrl.get_portfolio_summary()
print(f"Capital: {summary['starting_capital']}")
print(f"Current Value: {summary['total_value']}")
print(f"P&L: {summary['total_pnl']}")
```

## 3. Fetch Market Data

```python
# Create market controller
market_ctrl = MarketController(logger=logger)

# Get market configuration
nse_config = market_ctrl.get_market_config("NSE")
print(f"Market Open: {nse_config['market_open']}")
print(f"Market Close: {nse_config['market_close']}")

# Get watchlist
watchlist = market_ctrl.get_watchlist("NSE")
print(f"Symbols: {watchlist[:5]}")

# Fetch quote for a symbol
quote = market_ctrl.fetch_quote("INFY.NS", "NSE")
if quote:
    print(f"INFY: {quote['price']} ({quote['pchange']:+.2f}%)")
```

## 4. Execute Trades

```python
from stockmarket.models import TradeType

# Get charges information
charges = market_ctrl.get_intraday_charges("NSE")
print(f"Commission: {charges['commission_pct']}%")

# Open a long position
try:
    trade = portfolio_ctrl.open_trade(
        symbol="RELIANCE.NS",
        trade_type="LONG",
        qty=10,
        entry_price=2500,
        entry_charges=75
    )
    logger.success(f"Opened trade: {trade.symbol}")
    
    # Save to storage
    portfolio_ctrl.save_to_storage()
    
except ValueError as e:
    logger.error(f"Trade failed: {e}")
```

## 5. Close Positions

```python
# Close an open position
try:
    closed_trade = portfolio_ctrl.close_trade(
        symbol="RELIANCE.NS",
        trade_type="LONG",
        exit_price=2600,
        exit_charges=78
    )
    
    if closed_trade:
        logger.info(f"Closed trade. PnL: {closed_trade.pnl:.2f} ({closed_trade.pnl_pct:.2f}%)")
        portfolio_ctrl.save_to_storage()
        
except ValueError as e:
    logger.error(f"Close failed: {e}")
```

## 6. Display Portfolio

```python
import pandas as pd
from stockmarket.views import (
    render_portfolio_summary,
    render_positions_table,
    render_trades_table,
    render_app_logs
)

# Get current prices (fetch from market)
current_prices = market_ctrl.get_all_prices()

# Display summary metrics
summary = portfolio_ctrl.get_portfolio_summary(current_prices)
render_portfolio_summary(summary)

# Display open positions
positions_df = portfolio_ctrl.get_positions_dataframe(current_prices)
render_positions_table(positions_df)

# Display trade history
trades = portfolio_ctrl.get_trade_history(limit=10)
render_trades_table(trades)

# Display application logs
ui_logs = logger.get_ui_logs(limit=50)
render_app_logs(ui_logs)
```

## 7. Configuration Management

```python
# Access all configurations at once
config_loader = ConfigLoader()
all_configs = config_loader.load_all()

# Access specific configuration
app_config = config_loader.get_app_config()
print(f"App Name: {app_config['app']['name']}")

# Access trading parameters
trading_cfg = config_loader.get_trading_config()
print(f"Max Daily Trades: {trading_cfg['default_trading']['max_trades_per_day']}")

# Access database configuration
db_cfg = config_loader.get_database_config()
print(f"Storage Type: {db_cfg['storage']['type']}")
```

## 8. Trade Validation and Charges Calculation

```python
from stockmarket.controllers import TradeController

trade_ctrl = TradeController(logger=logger)

# Validate trade before execution
is_valid, error = trade_ctrl.validate_trade(
    symbol="INFY.NS",
    qty=10,
    price=1500,
    available_capital=50000,
    market="NSE"
)

if not is_valid:
    logger.error(f"Trade invalid: {error}")
else:
    # Calculate charges
    charges = trade_ctrl.calculate_charges(
        market="NSE",
        qty=10,
        price=1500,
        side="BUY",
        intraday_charges=charges_config
    )
    
    print(f"Commission: {charges['commission']:.2f}")
    print(f"Total Charges: {charges['total']:.2f} ({charges['percentage']:.4f}%)")
    
    # Calculate PnL for a hypothetical close
    pnl = trade_ctrl.calculate_pnl(
        entry_price=1500,
        exit_price=1550,
        qty=10,
        trade_type="LONG",
        entry_charges=45,
        exit_charges=47
    )
    
    print(f"Gross P&L: {pnl['gross_pnl']:.2f}")
    print(f"Net P&L: {pnl['net_pnl']:.2f}")
    print(f"Return: {pnl['pnl_pct']:.2f}%")
```

## 9. Storage Backend Switching

```python
# Switch storage with configuration (no code changes!)

# Option 1: File-based storage (development)
# In config/database_config.json:
# "storage": {"type": "file", "file_storage_path": ".data"}

# Option 2: SQLite (production single-user)
# In config/database_config.json:
# "storage": {"type": "sqlite", "database_path": ".database/trading.db"}

# Option 3: PostgreSQL (future multi-user)
# In config/database_config.json:
# "storage": {"type": "postgresql", ...}

# Code remains the same!
storage = StorageFactory.create_storage()  # Reads from config automatically
```

## 10. Complete Streamlit Dashboard Example

```python
import streamlit as st
from stockmarket.controllers import PortfolioController, MarketController
from stockmarket.utils import AppLogger, ConfigLoader
from stockmarket.views import (
    render_market_selector,
    render_sidebar_config,
    render_portfolio_summary,
    render_positions_table,
    render_trades_table,
    render_app_logs,
)

# Initialize session state
if 'logger' not in st.session_state:
    st.session_state.logger = AppLogger("Dashboard")
    st.session_state.config_loader = ConfigLoader()
    st.session_state.market_ctrl = MarketController(logger=st.session_state.logger)
    st.session_state.portfolio_ctrl = PortfolioController(
        portfolio_id="user_1",
        initial_capital=200000,
        logger=st.session_state.logger
    )
    st.session_state.portfolio_ctrl.load_from_storage()

logger = st.session_state.logger
market_ctrl = st.session_state.market_ctrl
portfolio_ctrl = st.session_state.portfolio_ctrl

# Title and market selector
st.title("📈 Trading Strategy Simulator")
selected_market = render_market_selector()

# Sidebar configuration
with st.sidebar:
    config = render_sidebar_config({
        'starting_capital': 200000,
        'max_trades_per_day': 1,
        'risk_per_trade_pct': 0.5,
        'stop_loss_pct': 0.4,
        'take_profit_pct': 0.8,
        'allow_short': False,
    })

# Main area: Portfolio summary
current_prices = market_ctrl.get_all_prices()
summary = portfolio_ctrl.get_portfolio_summary(current_prices)
render_portfolio_summary(summary)

# Positions and trades
col1, col2 = st.columns(2)

with col1:
    positions_df = portfolio_ctrl.get_positions_dataframe(current_prices)
    render_positions_table(positions_df)

with col2:
    trades = portfolio_ctrl.get_trade_history(limit=10)
    render_trades_table(trades)

# Logs at bottom
ui_logs = logger.get_ui_logs(limit=50)
render_app_logs(ui_logs)
```

## 11. Error Handling and Logging

```python
from stockmarket.utils import AppLogger

logger = AppLogger("TradingModule")

try:
    # Perform some operation
    quote = market_ctrl.fetch_quote("INFY.NS", "NSE")
    
    if not quote:
        logger.warning("Could not fetch quote for INFY.NS")
    else:
        logger.info(f"Fetched price: {quote['price']}")
    
except Exception as e:
    logger.error(f"Unexpected error: {e}")

# Get logs for display
logs = logger.get_ui_logs(limit=50)
for log in logs:
    st.markdown(log)

# Or use message functions from views
from stockmarket.views import render_error_message, render_success_message

render_error_message("Transaction failed: Insufficient capital")
render_success_message("Trade executed successfully")
```

## 12. Models Usage Examples

```python
from stockmarket.models import Portfolio, Trade, TradeType, TradePosition

# Create portfolio
portfolio = Portfolio(initial_capital=100000)

# Create a trade
trade = Trade(
    symbol="INFY.NS",
    trade_type=TradeType.LONG,
    entry_price=1500,
    entry_time=pd.Timestamp.now(),
    qty=10,
    entry_charges=45
)

# Open position
portfolio.open_position(trade)

# Get portfolio summary
summary = portfolio.get_summary(current_prices={'INFY.NS': 1550})
print(f"Total P&L: {summary['total_pnl']}")

# Get positions as DataFrame
positions_df = portfolio.get_portfolio_dataframe(current_prices)
print(positions_df)
```

## Configuration Files Reference

### app_config.json
```json
{
  "logging": {
    "level": "INFO",
    "max_log_entries": 100
  },
  "cache": {
    "ttl_current_day_seconds": 300,
    "ttl_historical_seconds": 21600
  }
}
```

### market_config.json
```json
{
  "markets": {
    "NSE": {
      "timezone": "Asia/Kolkata",
      "market_open": "09:15",
      "watchlist": ["RELIANCE.NS", "INFY.NS", ...],
      "intraday_charges": {
        "commission_pct": 0.03,
        "gst_pct": 18
      }
    }
  }
}
```

### trading_config.json
```json
{
  "default_trading": {
    "starting_capital": 200000.0,
    "max_trades_per_day": 1,
    "risk_per_trade_pct": 0.5
  }
}
```

### database_config.json
```json
{
  "storage": {
    "type": "sqlite",
    "database_path": ".database/trading.db"
  }
}
```

---

## Tips & Best Practices

1. **Initialize once, reuse**: Create controllers in session_state to avoid recreating
2. **Always save**: Call `portfolio_ctrl.save_to_storage()` after trades
3. **Use logging**: Always log significant operations for debugging
4. **Check validity**: Always validate trades before execution
5. **Handle errors**: Wrap API calls in try-catch with specific error logging
6. **Cache prices**: Use market controller's cache instead of fetching repeatedly
7. **Configuration first**: Check config files before hardcoding values
8. **Reuse components**: Use views package components across dashboards

---

For comprehensive architecture details, see **DesignPattern.md**
For refactoring overview, see **REFACTORING_SUMMARY.md**
