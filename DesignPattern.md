# Trading Strategy Simulator - Architecture & Design Pattern

## Overview

This document describes the refactored architecture of the Trading Strategy Simulator following the **MVC (Model-View-Controller)** pattern with clear separation of concerns, database abstraction, and comprehensive configuration management.

## Architecture Goals

1. **Maintainability**: Clean separation of concerns with well-defined responsibilities
2. **Scalability**: Support for multiple storage backends (file-based, SQLite, PostgreSQL)
3. **Configurability**: All hardcoded values removed to configuration files
4. **Testability**: Each layer can be tested independently
5. **Flexibility**: Easy to extend with new features or integrate with external systems

---

## Directory Structure

```
src/
├── stockmarket/
│   ├── models/                   # Business logic and data structures
│   │   ├── __init__.py
│   │   ├── trade.py             # Trade and TradePosition models
│   │   ├── portfolio.py         # Portfolio management
│   │   └── market.py            # Market data model
│   │
│   ├── controllers/              # Application logic layer
│   │   ├── __init__.py
│   │   ├── portfolio_controller.py  # Portfolio operations
│   │   ├── market_controller.py     # Market data operations
│   │   └── trade_controller.py      # Trade execution logic
│   │
│   ├── views/                    # Streamlit UI components
│   │   ├── __init__.py
│   │   └── components.py        # Reusable UI components
│   │
│   ├── persistence/              # Data storage abstraction layer
│   │   ├── __init__.py          # Abstract StorageBackend class
│   │   ├── file_storage.py      # JSON file-based storage
│   │   ├── db_storage.py        # SQLite database storage
│   │   └── factory.py           # Storage factory pattern
│   │
│   ├── utils/                    # Utility functions and helpers
│   │   ├── __init__.py
│   │   ├── constants.py         # Application constants
│   │   ├── config_loader.py     # Configuration file loader
│   │   ├── logger.py            # Dual-channel logging system
│   │   └── validators.py        # Input validation helpers
│   │
│   └── [existing files]         # Legacy files (strategy.py, etc.)
│
└── config/                       # Configuration files
    ├── app_config.json          # Application settings
    ├── market_config.json       # Market configurations & watchlists
    ├── trading_config.json      # Trading parameters
    └── database_config.json     # Database configuration

dashboard_simple.py              # Main Streamlit application (refactored)
dashboard.py                     # Alternative UI (refactored)
```

---

## Architecture Layers

### 1. **Models Layer** (`src/stockmarket/models/`)

Represents core business entities and logic.

#### Classes:
- **Trade**: Represents a single trade transaction with entry/exit logic
- **TradePosition**: Aggregates multiple trades in same symbol
- **Portfolio**: Manages collection of positions, capital, and P&L
- **MarketData**: Cache for market quotes and price information
- **MarketQuote**: Individual market quote data structure

#### Responsibilities:
- Encapsulate business rules (e.g., PnL calculations)
- Provide data validation
- Calculate derived values (P&L %, ROI, etc.)
- Remain independent of storage mechanism

#### Example Usage:
```python
from stockmarket.models import Portfolio, Trade, TradeType

portfolio = Portfolio(initial_capital=200000)
trade = Trade(symbol="INFY.NS", trade_type=TradeType.LONG, 
              entry_price=1000, entry_time=datetime.now(), qty=10)
portfolio.open_position(trade)
```

---

### 2. **Persistence Layer** (`src/stockmarket/persistence/`)

Abstraction layer for data storage supporting multiple backends.

#### Architecture:
- **StorageBackend** (ABC): Abstract interface for storage operations
- **FileStorageBackend**: JSON file-based implementation
- **DatabaseStorageBackend**: SQLite implementation
- **StorageFactory**: Factory for creating appropriate backend

#### Supported Operations:
- Save/load portfolio state
- Save/load trades
- Save/load positions
- Delete portfolio
- List portfolios

#### Key Design:
- Backend switching by configuration (no code changes needed)
- Future support for PostgreSQL without UI changes
- Automatic schema creation and migrations

#### Example Usage:
```python
from stockmarket.persistence.factory import StorageFactory

# Create storage based on config
storage = StorageFactory.create_storage(storage_type='sqlite')
storage.initialize()

# Save portfolio state
storage.save_portfolio_state(portfolio_id, state_data)

# Load trades
trades = storage.load_trades(portfolio_id, limit=100)
```

#### Database Schema (SQLite):
```sql
portfolios (id, created_at, last_updated, capital, current_pnl)
portfolio_states (portfolio_id, starting_capital, current_capital, total_pnl, state_json, last_updated)
trades (id, portfolio_id, symbol, trade_type, entry_price, entry_time, qty, ...)
positions (id, portfolio_id, symbol, trade_type, quantity, avg_price, ...)
```

---

### 3. **Controllers Layer** (`src/stockmarket/controllers/`)

Application logic coordinating between models and views.

#### Controllers:

##### PortfolioController
- Manages portfolio operations (open/close trades)
- Handles persistence coordination
- Provides portfolio summaries and reports

```python
controller = PortfolioController(portfolio_id="user_1", 
                                 initial_capital=200000)
controller.load_from_storage()
trade = controller.open_trade(symbol="RELIANCE.NS", trade_type="LONG",
                              qty=10, entry_price=2500, entry_charges=75)
controller.save_to_storage()
```

##### MarketController
- Fetches and caches market quotes
- Manages market configurations and watchlists
- Calculates intraday charges

```python
market_ctrl = MarketController()
nse_config = market_ctrl.get_market_config("NSE")
watchlist = market_ctrl.get_watchlist("NSE")
quote = market_ctrl.fetch_quote("INFY.NS", "NSE")
```

##### TradeController
- Validates trades before execution
- Calculates charges and PnL
- Manages trade lifecycle

```python
trade_ctrl = TradeController()
is_valid, error = trade_ctrl.validate_trade("INFY.NS", qty=10, 
                                            price=1000, 
                                            available_capital=50000)
charges = trade_ctrl.calculate_charges("NSE", qty=10, price=1000)
```

---

### 4. **Views Layer** (`src/stockmarket/views/`)

Streamlit UI components for rendering the interface.

#### Reusable Components:
- `render_market_selector()`: Market selection dropdown
- `render_sidebar_config()`: Configuration inputs
- `render_portfolio_summary()`: Portfolio metrics display
- `render_positions_table()`: Open positions table
- `render_trades_table()`: Trade history table
- `render_app_logs()`: Application logs display
- `render_watchlist_selector()`: Symbol selection
- `render_price_chart()`: Price visualization
- Message rendering: error, success, warning, info

#### Design Pattern:
- Components are stateless and accept data as parameters
- Streamlit session_state manages input state persistence
- Reusable across multiple dashboards

#### Example Usage:
```python
import streamlit as st
from stockmarket.views import (render_sidebar_config, render_portfolio_summary)

# Render configuration sidebar
config = render_sidebar_config(trading_config)

# Render portfolio metrics
render_portfolio_summary(portfolio_summary)
```

---

### 5. **Utilities Layer** (`src/stockmarket/utils/`)

Common utilities and helpers.

#### Components:

##### ConfigLoader
Loads and manages JSON configuration files.

```python
from stockmarket.utils import ConfigLoader

loader = ConfigLoader()
market_config = loader.get_market_config()
trading_config = loader.get_trading_config()
db_config = loader.get_database_config()
```

##### AppLogger
Dual-channel logging (console + UI display).

```python
from stockmarket.utils import AppLogger

logger = AppLogger("MyModule")
logger.info("Starting process")
logger.error("An error occurred")

# Get logs for UI display
ui_logs = logger.get_ui_logs(limit=50)
```

##### Constants
Application-wide constants and enumerations.

```python
from stockmarket.utils.constants import (
    TRADE_TYPE_LONG, TRADE_TYPE_SHORT,
    MARKET_NSE, MARKET_US,
    SUPPORTED_INTERVALS
)
```

---

## Configuration Files

### `config/app_config.json`
Application-level settings: logging, streaming, caching, API endpoints.

### `config/market_config.json`
Market-specific configurations: hours, watchlists, intraday charges.

### `config/trading_config.json`
Trading parameters: starting capital, risk levels, strategy parameters.

### `config/database_config.json`
Storage configuration: type (file/sqlite/postgres), connection details.

---

## Data Flow

### Trade Execution Flow
```
View (UI Input)
    ↓
TradeController (Validation + Charge Calculation)
    ↓
PortfolioController (Portfolio Update)
    ↓
Portfolio Model (Update Position & Capital)
    ↓
StorageBackend (Persist Trade & State)
    ↓
Database/File System
```

### Market Data Flow
```
View (Symbol Selection)
    ↓
MarketController (Fetch Quote from API)
    ↓
MarketData Model (Cache Quote)
    ↓
View (Display Price)
```

---

## Key Design Patterns

### 1. **Factory Pattern**
`StorageFactory` creates appropriate storage backend based on configuration.

### 2. **Adapter Pattern**
`StorageBackend` interface adapts different storage implementations (file, DB).

### 3. **Singleton Pattern** (Implicit)
Controllers are typically instantiated once and reused across the session.

### 4. **Strategy Pattern**
Different market APIs (NSE, yfinance) can be swapped via market selection.

### 5. **Observer Pattern** (Streamlit)
Session state changes automatically trigger re-renders.

---

## Configuration-Driven Design

All hardcoded values removed and moved to configuration files:

| Hardcode Before | Config After | Location |
|-----------------|--------------|----------|
| Watchlist arrays | `watchlist` key | `market_config.json` |
| Market hours | `market_open`, `market_close` | `market_config.json` |
| Commission rates | `intraday_charges` | `market_config.json` |
| Starting capital | `starting_capital` | `trading_config.json` |
| Cache TTL | `cache` section | `app_config.json` |
| File paths | `file_storage_path` | `database_config.json` |
| API endpoints | `api` section | `app_config.json` |

---

## Database Support

### Current: SQLite
- Zero configuration
- Good for single-user/small deployments
- File-based (.database/trading.db)
- WAL mode for better concurrency

### Future: PostgreSQL
- Multi-user support
- Better for production deployments
- Configuration via `database_config.json`
- No code changes needed - use `StorageFactory`

### Switching backends:
```json
{
  "storage": {
    "type": "postgresql"  // Change from "sqlite" to "postgresql"
  }
}
```

---

## Migration Guide

### From Old Dashboard to New Architecture

#### Before (Monolithic):
```python
# dashboard_simple.py - 3300+ lines
WATCHLIST_NSE = [...]  # Hardcoded
MARKET_CONFIG = {...}   # Hardcoded
# All logic mixed in Streamlit callbacks
```

#### After (Modular):
```python
# dashboard_simple.py - Refactored to ~500 lines
from stockmarket.controllers import PortfolioController, MarketController
from stockmarket.views import render_portfolio_summary
from stockmarket.utils import AppLogger, ConfigLoader

# Logic separated into controllers
# UI components reusable
# Configuration external
```

---

## Testing Strategy

### Model Testing
- Unit test Trade, Portfolio, MarketData classes
- Test calculations (PnL, charges)
- Test state transitions

### Controller Testing
- Mock storage backend
- Test validation logic
- Test charge calculations

### View Testing
- Component rendering with various data
- Session state management
- Error handling

### Integration Testing
- End-to-end trading workflow
- Storage persistence
- Configuration loading

---

## Extension Points

### Adding New Markets
1. Add market configuration to `market_config.json`
2. Extend `MarketController.fetch_quote()` with new API integration
3. Update tests

### Adding New Storage Backend
1. Create class inheriting `StorageBackend`
2. Implement required abstract methods
3. Register in `StorageFactory.create_storage()`
4. Update configuration

### Adding New Strategy
1. Create module in appropriate package
2. Implement strategy logic
3. Integrate via controller
4. Add configuration parameters

---

## Deployment Checklist

- [ ] All hardcoded values in configuration files
- [ ] Test with both file and SQLite backends
- [ ] Verify logging system works in UI
- [ ] Test market data fetching for both NSE and US
- [ ] Validate portfolio persistence across refreshes
- [ ] Check error handling and recovery
- [ ] Performance test with large number of trades
- [ ] Documentation updated

---

## Benefits of This Architecture

1. **Maintainability**: Each component has single responsibility
2. **Testability**: Components are loosely coupled
3. **Scalability**: Easy to add new features or backends
4. **Flexibility**: Configuration-driven, not hardcoded
5. **Reusability**: Views, controllers, models can be reused
6. **Clarity**: Clear data flow and communication paths
7. **Future-proof**: Easy to migrate to production database
8. **Team-friendly**: Different developers can work on different layers

---

## Quick Start

### 1. Configuration
Update `config/` directory files for your setup.

### 2. Initialize Database
```python
from stockmarket.persistence.factory import StorageFactory
storage = StorageFactory.create_storage()
storage.initialize()
```

### 3. Create Portfolio
```python
from stockmarket.controllers import PortfolioController
controller = PortfolioController("user_1", 200000)
```

### 4. Execute Trade
```python
trade = controller.open_trade(
    symbol="INFY.NS",
    trade_type="LONG",
    qty=10,
    entry_price=1500,
    entry_charges=45
)
```

### 5. Display in UI
```python
from stockmarket.views import render_portfolio_summary
render_portfolio_summary(controller.get_portfolio_summary())
```

---

## Troubleshooting

### Issue: Database not found
**Solution**: Run `storage.initialize()` to create schema.

### Issue: Configuration not loading
**Solution**: Check `config/` directory exists and files are valid JSON.

### Issue: Market data unavailable
**Solution**: Check API connectivity and rate limits.

### Issue: Session state lost on refresh
**Solution**: Verify portfolio is saved to storage after each transaction.

---

## References

- MVC Pattern: https://en.wikipedia.org/wiki/Model%E2%80%93view%E2%80%93controller
- Design Patterns: https://refactoring.guru/design-patterns
- SQLite WAL Mode: https://www.sqlite.org/wal.html
- Streamlit Session State: https://docs.streamlit.io/library/api-reference/session-state
