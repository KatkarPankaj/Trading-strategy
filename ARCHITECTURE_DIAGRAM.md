```mermaid
flowchart LR
  subgraph EP["Entrypoints"]
    PYM["`python -m stockmarket`"]
    Main["src/stockmarket/__main__.py"]
    CLI["src/stockmarket/cli.py"]
    App["app.py"]
    SimpleDash["dashboard_simple.py"]
    LegacyDash["dashboard.py"]
    WebApp["webapp.py"]
  end

  subgraph Core["Core Trading Engine"]
    Config["src/stockmarket/config.py"]
    Data["src/stockmarket/data.py"]
    Strategy["src/stockmarket/strategy.py"]
    Backtest["src/stockmarket/backtest.py"]
    Sweep["src/stockmarket/sweep.py"]
    Optim["src/stockmarket/optimizer.py"]
    ML["src/stockmarket/market_learning.py"]
  end

  subgraph Signals["Signals / Pricing Rules"]
    Ranking["src/stockmarket/ranking.py"]
    SimpleSignals["src/stockmarket/simple_signals.py"]
    Charges["src/stockmarket/charges.py"]
  end

  subgraph Quotes["Market Data Provider Layer"]
    QuoteSvc["src/stockmarket/quotes/service.py"]
    QuoteNSE["src/stockmarket/quotes/nse.py"]
    QuoteFH["src/stockmarket/quotes/finnhub_quotes.py"]
    QuoteCache["src/stockmarket/quotes/ttl_cache.py"]
    QuoteTypes["src/stockmarket/quotes/types.py"]
  end

  subgraph Domain["Domain / Layered Refactor (in-code package)"]
    TradeModel["src/stockmarket/models/trade.py"]
    PortfolioModel["src/stockmarket/models/portfolio.py"]
    MarketModel["src/stockmarket/models/market.py"]
    Logger["src/stockmarket/utils/logger.py"]
    ConfigLoader["src/stockmarket/utils/config_loader.py"]
    PortfolioCtrl["src/stockmarket/controllers/portfolio_controller.py"]
    MarketCtrl["src/stockmarket/controllers/market_controller.py"]
    TradeCtrl["src/stockmarket/controllers/trade_controller.py"]
    StorageBackend["src/stockmarket/persistence/__init__.py"]
    StorageFactory["src/stockmarket/persistence/factory.py"]
    FileStore["src/stockmarket/persistence/file_storage.py"]
    DbStore["src/stockmarket/persistence/db_storage.py"]
    Views["src/stockmarket/views/components.py + theme.py"]
  end

  subgraph Tests["Tests"]
    TQuotes["tests/test_quotes.py"]
    TCharges["tests/test_charges.py"]
    TRanking["tests/test_ranking.py"]
    TData["tests/test_data_batch.py"]
    TSignals["tests/test_simple_signals.py"]
    TSimple["tests/test_dashboard_simple.py"]
  end

  subgraph IO["External IO"]
    ConfigFiles["`config/*.json`"]
    FS["`.data/` and `.database/`"]
    Outputs["`outputs/`"]
  end

  PYM --> Main --> CLI
  CLI --> Config --> ConfigLoader
  CLI --> Data
  CLI --> Strategy
  CLI --> Backtest
  CLI --> Sweep
  CLI --> Optim
  CLI --> ML

  App --> SimpleDash
  WebApp --> Backtest
  WebApp --> Data
  WebApp --> Strategy
  WebApp --> Sweep
  LegacyDash --> Config
  LegacyDash --> Data
  LegacyDash --> Strategy
  LegacyDash --> Ranking
  LegacyDash --> Charges
  LegacyDash --> QuoteSvc

  SimpleDash --> QuoteSvc
  SimpleDash --> SimpleSignals
  SimpleDash --> Charges
  SimpleDash -.-> ML
  SimpleDash -.-> Optim

  QuoteSvc --> QuoteNSE
  QuoteSvc --> QuoteFH
  QuoteSvc --> QuoteCache
  QuoteNSE --> QuoteTypes
  QuoteFH --> QuoteTypes

  Data --> Data
  Strategy --> Config
  Backtest --> Strategy
  Backtest --> Data
  Sweep --> Backtest
  Optim --> Data
  Optim --> Backtest
  Optim --> Strategy
  ML --> Data
  ML --> Config

  PortfolioCtrl --> PortfolioModel
  PortfolioCtrl --> TradeModel
  PortfolioCtrl --> StorageFactory
  PortfolioCtrl --> Logger

  MarketCtrl --> MarketModel
  MarketCtrl --> ConfigLoader
  MarketCtrl --> QuoteSvc
  TradeCtrl --> TradeModel
  TradeCtrl --> Logger

  StorageFactory --> FileStore
  StorageFactory --> DbStore
  FileStore --> FS
  DbStore --> FS
  ConfigLoader --> ConfigFiles

  Views --> Logger

  TQuotes --> QuoteSvc
  TQuotes --> QuoteNSE
  TQuotes --> QuoteTypes
  TCharges --> Charges
  TRanking --> Ranking
  TData --> Data
  TSignals --> SimpleSignals
  TSimple --> SimpleDash
```
