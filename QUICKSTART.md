# Quick Start Guide

The previous "MVC architecture" quick-start (`PortfolioController`,
`MarketController`, `TradeController`, `StorageFactory`, `stockmarket.models`,
`stockmarket.views.components`, etc.) described an abandoned layer that has
been removed from the codebase. The supported entry points are:

## Run the dashboard (paper trading)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

`app.py` loads the Simple dashboard from `dashboard_simple.py`. There is no
sidebar toggle into a "complex scanner"; the legacy `dashboard.py` is now a
deprecation stub that redirects you here.

## Run the CLI (backtests, signals, optimization)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli backtest --config config.json
.\.venv\Scripts\python.exe -m stockmarket.cli signals  --config config.json
.\.venv\Scripts\python.exe -m stockmarket.cli optimize --config config.json --trade-file outputs/paper_trade_history.csv
```

See [README.md](README.md) for the full CLI reference (sweep, replay-best,
optimize feature engineering, etc.) and [DOCUMENTATION.md](DOCUMENTATION.md)
for an overview of strategy logic, configuration, and paper-trading mechanics.

## Configuration

```powershell
Copy-Item config.example.json config.json
```

Adjust `config.json` for symbol, intervals, risk parameters, and capital.
For US data set `FINNHUB_API_KEY`; for NSE quotes no key is required.

## State and outputs

- Paper-trading state: `outputs/simple_paper_state*.json`
- Trade history (CSV): `outputs/paper_trade_history.csv`
- Daily PnL: `outputs/daily_pnl_history.csv`
- Sweep / optimize artifacts: `outputs/*.csv`, `outputs/*.json`
