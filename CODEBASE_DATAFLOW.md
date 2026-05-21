# Codebase Dataflow Diagrams

Last reviewed: 2026-05-21 (post-architecture-migration).

These Mermaid diagrams describe the runtime after the phase 1–9 migration on `arch-migration-plan`. The legacy MVC stack is gone; the supported runtime is `app.py` → `dashboard_simple.render_simple_dashboard()` over the `domain` / `state` / `cycle` / `persistence` / `views` packages.

## Runtime entrypoints

```mermaid
flowchart TD
  User["User"]

  subgraph UI["Streamlit UI Entrypoints"]
    App["app.py\nsupported launcher"]
    Simple["dashboard_simple.py\nprimary simulator"]
    Legacy["dashboard.py\n31-line deprecation stub"]
    WebApp["src/stockmarket/webapp.py\nresearch UI (banner)"]
  end

  subgraph CLI["CLI Entrypoints"]
    Module["python -m stockmarket"]
    CliPy["src/stockmarket/cli.py"]
  end

  User -->|"streamlit run app.py"| App
  App --> Theme["src/stockmarket/views/theme.py"]
  App --> Simple

  User -. "shows banner & stops" .-> Legacy
  User -.-> WebApp

  User -->|"python -m stockmarket ..."| Module
  Module --> CliPy
```

## Supported dashboard dataflow

```mermaid
flowchart TD
  App["app.py"] --> Simple["dashboard_simple.render_simple_dashboard"]

  subgraph Orchestration["dashboard_simple.py owns"]
    UIShell["Streamlit shell + sidebar"]
    MarketSelect["market selection (NSE / US)"]
    AutoRun["auto-trading orchestration"]
  end

  Simple --> UIShell
  UIShell --> MarketSelect

  subgraph Views["views/ (UI rendering)"]
    Top["simple_top_panels"]
    Activity["simple_activity_and_logs"]
    Tables["simple_signals_tables"]
    Tomorrow["simple_tomorrow_plan"]
    Portfolio["simple_portfolio_metrics"]
    Refresh["simple_auto_refresh"]
  end

  Simple --> Top
  Simple --> Activity
  Simple --> Tables
  Simple --> Tomorrow
  Simple --> Portfolio
  Simple --> Refresh

  subgraph Cycle["cycle/ (trade execution, USE_TRADING_CYCLE=1)"]
    Factory["cycle.factory.build_services_from_session"]
    Runner["cycle.runner.run_cycle"]
    Steps["steps/: prices, gates, entries, exits, counters"]
    Adapters["adapters/: streamlit_broker, session_repo,\nsession_prices, dashboard_signals,\nlive_clock, log_history"]
    Scoring["cycle/scoring.py"]
  end

  AutoRun --> Factory
  Factory --> Runner
  Runner --> Steps
  Runner --> Adapters
  Runner --> Scoring

  subgraph Bridge["state/"]
    SessionBridge["session_bridge.py\n(dict ⇄ domain dataclasses)"]
  end

  subgraph Domain["domain/"]
    Types["types.py: PaperState,\nTradeLogEntry (tradebookid),\nPosition, DailyCounters,\nAppSettings dataclasses,\nPortfolioSnapshot"]
    Scorer["scorer.py: SymbolScorer protocol"]
  end

  Simple --> SessionBridge
  Adapters --> SessionBridge
  SessionBridge --> Types
  Scoring --> Scorer

  subgraph Persistence["persistence/ (USE_PAPER_REPO=1)"]
    Factory2["paper_repo.get_paper_repo()"]
    JsonRepo["JsonPaperRepo"]
    SqliteRepo["SqlitePaperRepo"]
    DualWrite["_DualWriteSqlitePaperRepo\n(dual-write / fallback)"]
  end

  Adapters --> Factory2
  Factory2 --> JsonRepo
  Factory2 --> SqliteRepo
  Factory2 --> DualWrite
  DualWrite --> SqliteRepo
  DualWrite --> JsonRepo

  subgraph ML["ml/ (gated by DISABLE_ML_SCORER)"]
    Null["NullSymbolScorer"]
    Sklearn["SklearnSymbolScorer"]
    Bias["BiasOverlayScorer"]
  end

  Scoring --> Null
  Scoring --> Sklearn
  Scoring --> Bias

  subgraph DataPath["Quotes & data"]
    Quotes["stockmarket.quotes.QuoteService"]
    SimpleSignals["simple_signals"]
    Charges["charges"]
    Ranking["ranking"]
  end

  Simple --> Quotes
  Simple --> SimpleSignals
  Simple --> Charges
  Simple --> Ranking
  Adapters --> Quotes

  subgraph Files["Generated state"]
    JsonFiles["outputs/simple_paper_state*.json"]
    Sqlite[".database/paper_state.db"]
  end

  JsonRepo <--> JsonFiles
  SqliteRepo <--> Sqlite
```

## Persistence factory branching

```mermaid
flowchart LR
  Caller["dashboard / cycle adapter"] --> Factory["get_paper_repo(path, market)"]
  Factory --> Resolve["_resolve_backend()"]
  Resolve -->|"PAPER_REPO_BACKEND=json\n(or unset → default)"| JsonRepo["JsonPaperRepo\noutputs/simple_paper_state*.json"]
  Resolve -->|"PAPER_REPO_BACKEND=sqlite"| SqliteCheck{"DUAL_WRITE\nor\nFALLBACK_JSON ?"}
  SqliteCheck -- "neither" --> SqliteRepo["SqlitePaperRepo\n.database/paper_state.db"]
  SqliteCheck -- "either flag set" --> Wrapper["_DualWriteSqlitePaperRepo\nprimary=SqliteRepo\nshadow=JsonRepo"]
  Wrapper --> SqliteRepo
  Wrapper -. "fallback on miss" .-> JsonRepo
  Wrapper -. "dual-write on save" .-> JsonRepo
```

## Backtest dataflow

```mermaid
flowchart LR
  User["User"] --> Cli["src/stockmarket/cli.py"]
  Cli --> Config["config.json → TradingConfig"]

  Cli --> BacktestCmd["backtest"]
  Cli --> SignalsCmd["signals"]
  Cli --> SweepCmd["sweep"]
  Cli --> ReplayCmd["replay-best"]
  Cli --> OptimizeCmd["optimize"]

  subgraph Engine["Research engine"]
    Data["data.py"]
    Strategy["strategy.py"]
    LegacyBacktest["backtest.py\n(legacy callable)"]
    Sweep["sweep.py"]
  end

  subgraph CycleBacktest["backtest/ (BACKTEST_USE_CYCLE=1)"]
    BTRun["run_backtest_via_cycle()"]
    BTAdapters["adapters/: clock,\nbroker, signals"]
    BTSteps["backtest/steps.py + sizer.py"]
  end

  subgraph Optim["optimization/"]
    Trades["trade_history.py"]
    Features["features.py"]
    Reports["reports.py"]
  end

  BacktestCmd --> Data
  Data --> Strategy
  Strategy --> LegacyBacktest
  Strategy --> BTRun
  BTRun --> BTSteps
  BTRun --> BTAdapters
  Sweep --> LegacyBacktest

  OptimizeCmd --> Trades
  Trades --> Features
  Features --> Reports

  subgraph Outputs["outputs/"]
    Trades2["trades_*.csv"]
    Sweeps["sweep_*.csv"]
    OptReports["*_recommendations.json,\n*_symbol_scores.csv,\n*_feature_scores.csv,\n*_model_feature_importance.csv"]
  end

  BacktestCmd --> Trades2
  SweepCmd --> Sweeps
  OptimizeCmd --> OptReports
```

## Migration script (one-shot, retained as future-proofing)

```mermaid
flowchart LR
  Op["operator"] --> Script["scripts/migrate_paper_state_json_to_sqlite.py"]
  Script --> JsonInput["outputs/simple_paper_state*.json"]
  Script --> SqliteOut[".database/paper_state.db"]
  JsonInput --> JsonRepo["JsonPaperRepo (read)"]
  JsonRepo --> SqliteRepo["SqlitePaperRepo (write)"]
  SqliteRepo --> SqliteOut
```

The script is not on the active runtime path — it exists for the eventual SQLite default-flip rollout.

## Source-of-truth pointers

- `MIGRATION.md` — phase-by-phase record of what shipped and why.
- `CODEBASE_STRUCTURE.md` — package layout, feature flags, runtime flow narrative.
- `README.md` — how to run the app and CLI.
