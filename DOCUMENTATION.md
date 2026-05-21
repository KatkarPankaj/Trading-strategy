# Stock Market Trading Simulator — Complete Documentation

## Table of Contents
1. [Tool Overview](#tool-overview)
2. [Architecture & Files](#architecture--files)
3. [Trading Strategy](#trading-strategy)
4. [Configuration](#configuration)
5. [Features Explained](#features-explained)
6. [How Stock Selection Works](#how-stock-selection-works)
7. [Paper Trading Simulator](#paper-trading-simulator)
8. [Edge Cases & Exceptions](#edge-cases--exceptions)
9. [Learning Agent](#learning-agent)
10. [Setup & Usage](#setup--usage)

---

## Tool Overview

**Purpose**: A personal-use **intraday stock trading simulator** for the NSE/BSE (Indian markets) that helps traders test algorithmic strategies with **paper money** before risking real capital.

**Key Goals**:
- Start with a defined budget (default: ₹200,000)
- Scan market data and generate intraday buy/sell signals automatically
- Execute paper trades without real money
- Track portfolio, PnL, and trade history
- Learn from past trades to improve future signal quality

**Why it exists**: Zerodha's Kite platform doesn't offer algorithmic trading, and manual trading is slow. This tool bridges that gap with local signal generation + paper simulation.

---

## Architecture & Files

### File Structure

```
StockMarket/
├── src/stockmarket/              # Core trading logic
│   ├── __init__.py              # Package init
│   ├── config.py                # TradingConfig dataclass
│   ├── strategy.py              # Signal generation (ORB + VWAP)
│   ├── data.py                  # Yahoo Finance data fetch + caching
│   ├── backtest.py              # Historical backtesting engine
│   ├── cli.py                   # Command-line interface
│   ├── sweep.py                 # Parameter sweep for tuning
│   ├── webapp.py                # Flask dashboard (legacy)
│   └── __main__.py              # Entry point
├── app.py                       # Streamlit launcher (loads dashboard_simple)
├── dashboard_simple.py          # Streamlit real-time paper simulator (PRIMARY)
├── dashboard.py                 # Deprecation stub (legacy complex scanner removed)
├── config.json                  # Runtime parameters (user-editable)
├── config.example.json          # Template config
├── requirements.txt             # Python dependencies
├── outputs/                     # Trade logs and state files
│   ├── simple_paper_state.json  # Live session state
│   ├── daily_pnl_history.csv    # Daily profit/loss history
│   ├── paper_trade_history.csv  # All closed trades
│   └── trades_*.csv             # Per-symbol trade logs
└── README.md                    # Quick start guide
```

### Core Modules Explained

| File | Purpose | Key Functions |
|------|---------|---|
| **config.py** | Configuration dataclass | Stores all parameters (SL%, TP%, risk%, interval, etc.) |
| **strategy.py** | Signal generation | `add_strategy_columns()` — adds VWAP, OR, volume spike, buy/sell signals |
| **data.py** | Data fetch & cache | `fetch_intraday_data()` — downloads 5-min OHLCV from Yahoo Finance, caches locally |
| **backtest.py** | Backtesting engine | `run_backtest()` — simulates trades on historical data with commissions & slippage |
| **dashboard_simple.py** | **Primary UI** | Real-time paper simulator with auto-trading, profit ladder, position tracking |
| **cli.py** | Console interface | Backtest runner, signal inspection |
| **sweep.py** | Parameter tuning | Tests multiple SL%, TP%, volume thresholds to find best combo |

### Why These Files Are Organized This Way

- **Separation of concerns**: Logic (strategy, backtest) separate from UI (dashboard)
- **Reusability**: `strategy.py` used by both backtest and paper simulator
- **Caching**: Data cached locally so repeated backtests don't hammer Yahoo Finance
- **Stateless data layer**: `data.py` and `strategy.py` don't depend on live state, only on config

---

## Trading Strategy

### Strategy Name: **Opening Range Breakout (ORB) with VWAP + Volume Filter**

### How It Works

#### 1. **Opening Range** (First 15 minutes of market)
- Record the HIGH and LOW of the first 15 minutes
- These become support/resistance levels for the entire day

#### 2. **Long Signal (BUY)** — triggered when:
✅ Time is after opening range BUT before 13:30 (entry cutoff)
✅ Close > Opening Range High
✅ Volume spike ≥ 1.2x average volume
✅ Close > VWAP (Volume-Weighted Average Price)

#### 3. **Short Signal (SELL)** — triggered when:
✅ Time is after opening range BUT before 13:30
✅ Close < Opening Range Low
✅ Volume spike ≥ 1.2x average volume
✅ Close < VWAP
✅ `allow_short` is enabled in config

### Why This Strategy?

| Why | Benefit |
|-----|---------|
| **ORB is mean-reversion** | Price often reverts to opening range after breaking out — profit from the reversion |
| **VWAP filter** | Ensures breakout has "institutional volume" behind it; filters noise |
| **Volume spike** | Avoids low-volume breakouts (more likely to fail) |
| **Entry cutoff (13:30)** | No new entries after 1:30 PM; time decay favors intraday traders |

### Example Trade Flow

```
09:15 - Market opens
09:15-09:30 - Wait for opening range HIGH=500, LOW=495
10:00 - Price closes at 502 with volume spike
10:00 - SIGNAL: Close (502) > OR_High (500) ✓, Volume spike ✓, Close > VWAP (501) ✓
10:00 - BUY at 502, set SL=498 (0.8%), TP=510 (1.6%)
10:05 - Price hits 510 → Auto SELL (TP hit)
10:05 - Trade closed, PnL = +₹40 (before commissions)
```

---

## Configuration

### config.json Parameters

```json
{
  "symbol": "RELIANCE.NS",          // NSE symbol (use .NS for NSE, .BO for BSE)
  "interval": "5m",                 // Candle size: "1m", "5m", "15m", "1h", "1d"
  "period": "30d",                  // Historical lookback for data
  "market_timezone": "Asia/Kolkata", // Critical: ensures correct times
  
  "opening_range_minutes": 15,      // First N minutes = support/resistance
  "entry_cutoff_time": "13:30",     // No new buys after this time
  "square_off_time": "15:15",       // Auto-exit all positions before market close
  "time_exit_minutes": 60,          // Exit trade if open for 60+ minutes
  
  "stop_loss_pct": 0.004,           // SL = entry × (1 - 0.4%)
  "take_profit_pct": 0.008,         // TP = entry × (1 + 0.8%)
  "risk_per_trade_pct": 0.005,      // Risk 0.5% of capital per trade
  "starting_capital": 200000.0,     // Initial budget
  "max_trades_per_day": 3,          // Max entries per day
  
  "commission_pct": 0.0003,         // Brokerage (0.03% each way typical for Zerodha)
  "slippage_pct": 0.0005,           // Slippage assumption (0.05%)
  
  "allow_short": false,             // Allow short-selling
  "volume_ma_window": 20,           // Average volume over past 20 candles
  "volume_spike_threshold": 1.2     // Volume must be 1.2x average to trigger signal
}
```

### How Parameters Affect Strategy

| Parameter | Effect |
|-----------|--------|
| ↑ SL% | Wider stops → fewer SL hits but larger losses when hit |
| ↓ TP% | Tighter targets → easier to hit but smaller profits |
| ↑ entry_cutoff_time | More time to enter → more trades but less time for them to mature |
| ↓ volume_spike_threshold | More signals (noisier) |
| ↑ risk_per_trade_pct | Bigger position sizes (riskier) |

---

## Features Explained

### 1. **Real-Time Signal Generation**
**File**: `dashboard_simple.py` → `_rank_signals()`

- Fetches latest 5-min candles for watchlist (12 stocks)
- Runs strategy logic on each symbol
- Ranks signals by strength (buy_score, sell_score)
- Returns TOP 5 buy signals, TOP 5 sell signals

**Watchlist** (hardcoded in dashboard_simple.py):
```python
WATCHLIST = [
    "IEX.NS", "BHEL.NS", "IRCTC.NS", "FEDERALBNK.NS",
    "IDFCFIRSTB.NS", "AUROPHARMA.NS", "COFORGE.NS", "BSE.NS",
    "CDSL.NS", "TATAPOWER.NS", "MOTHERSON.NS", "PERSISTENT.NS"
]
```

**Why these stocks?**
- Mid-cap, liquid (enough volume for intraday)
- Different sectors (banking, tech, pharma, infrastructure)
- ₹50–₹1,800 price range (avoids penny stocks + mega-caps)

### 2. **Paper Trading Simulator**
**File**: `dashboard_simple.py` → `_auto_paper_cycle()`

**What it does**:
- Monitors portfolio (holdings + shorts)
- Auto-exits on SL, TP, signal sell, or 3:15 PM (market close)
- Tracks cash, charges (commissions), and PnL
- Persists state to `simple_paper_state.json`

**State Tracking**:
```json
{
  "cash": 195000,                   // Available cash
  "start": 200000,                  // Starting capital
  "realized": 2500,                 // Closed trade PnL
  "charges": 150,                   // Total commissions
  "holdings": {                     // Open LONG positions
    "RELIANCE.NS": {
      "qty": 10,
      "avg": 2500,
      "stop": 2480,
      "target": 2550
    }
  },
  "shorts": {},                     // Open SHORT positions
  "log": [...]                      // Trade history
}
```

### 3. **Profit Ladder Lock** (NEW)
**File**: `dashboard_simple.py` → lines 681–750

**Logic**:
1. **Arm** when daily PnL reaches target (e.g., ₹4,000)
2. **Exit all** if PnL hits target + ₹2,000 (e.g., ₹6,000)
3. **Lock & exit** if PnL pulls back to target + ₹1,000 (e.g., ₹5,000)

**Why?** Protects against the "6,890 → 3,800 crash" you experienced. Once you hit a big profit, this rule exits automatically to avoid giving it all back.

### 4. **Learning Agent**
**File**: `dashboard_simple.py` → `_update_learning_memory()`, `_agent_tuning_plan()`

**Tracks**:
- Past closed trades (win rate, avg win, avg loss)
- Market regime (up/down/neutral)
- Volatility (high/low)

**Adjusts**:
- **Buy Score**: reduced if daily win rate < 30%
- **TP%**: increased if market is trending down (targets too tight)
- **SL%**: loosened if many trades hit SL early

**Goal**: Self-tune parameters based on daily results, not fixed values.

### 5. **Auto Refresh**
**File**: `dashboard_simple.py` → `_auto_refresh()`

Re-checks signals and trades every 20–120 seconds (configurable).

### 6. **Profit Tracking**
**File**: `dashboard_simple.py`

**Metrics displayed**:
- Start Capital, Current Equity, Cash, Open PnL, Realized PnL, Charges
- Daily target progress (e.g., "₹963 / ₹4,000 (24.1%)")
- Yesterday's PnL (loaded from `daily_pnl_history.csv`)

---

## How Stock Selection Works

### Step 1: Watchlist Definition (STATIC)
Hardcoded in `dashboard_simple.py`:
```python
WATCHLIST = ["IEX.NS", "BHEL.NS", "IRCTC.NS", "FEDERALBNK.NS",
             "IDFCFIRSTB.NS", "AUROPHARMA.NS", "COFORGE.NS", "BSE.NS",
             "CDSL.NS", "TATAPOWER.NS", "MOTHERSON.NS", "PERSISTENT.NS"]
```

**The watchlist is ALWAYS these 12 stocks.** It does NOT change based on market conditions.

**Criteria for inclusion** (when you edit):
- NSE/BSE symbols
- Liquid (10,000+ daily volume)
- Mid-cap or larger (₹50+)
- Different sectors (avoid concentration)
- Price range ₹50–₹1,800 (filters penny stocks + mega-caps)

### Step 2: Data Fetch (Per Refresh)
For each of the 12 symbols:
1. Download last 30 days of 5-min OHLCV
2. Cache locally to avoid repeated API calls
3. If cache fresh (<15 min old), use cached data
4. Refresh "last traded price" for open positions

### Step 3: Signal Generation (Per Symbol)
For each symbol, `add_strategy_columns()` creates:
- Opening Range High/Low (first 15 min of day)
- VWAP (Volume-Weighted Average Price, recalculated daily)
- Volume MA (20-period moving average)
- Buy/Short signal booleans (True if all conditions met)

### Step 4: Ranking (DYNAMIC)
Signals ranked by "strength" each cycle:
- **Buy Score** = base score + symbol bias (from learning agent) + market adjustments
- **Sell Score** = similar for exits
- **Results**: TOP 5 buy signals + TOP 5 sell signals displayed in UI

**The learning agent ADJUSTS scores based on**:
- Daily win rate (lower scores if <30% win rate)
- Market regime (uptrend = favor momentum, downtrend = favor reversals)
- Volatility (high vol = wider stops, lower buy score)

### Step 5: Filtering (DYNAMIC)
Before auto-trade entry, skip if:
- ❌ Already holding this symbol
- ❌ Buy score < your configured minimum (e.g., 40.0)
- ❌ Price outside your min/max range (e.g., <₹50 or >₹1,800)
- ❌ Max open positions reached (e.g., 3)
- ❌ Max trades per day reached (e.g., 5)
- ❌ Profit guard triggered (new entries blocked)
- ❌ Market is closed (weekends)

---

## Paper Trading Simulator

### Trade Execution Flow

```
User enables "Enable Auto Paper Trading":
│
├─ Every refresh cycle:
│   ├─ Fetch latest prices for all holdings
│   ├─ Check for SL/TP hits
│   │   └─ Auto-exit if hit
│   ├─ Check for signal exits (if enabled)
│   │   └─ Auto-exit if signal ready
│   ├─ Check for time exits (60+ min open)
│   │   └─ Auto-exit if hit
│   ├─ Check for profit ladder lock
│   │   └─ Exit all if ladder triggered
│   ├─ Check for 3:15 PM square-off
│   │   └─ Exit all positions
│   ├─ Check if new entries allowed
│   │   └─ Max positions? Max trades? Guard triggered?
│   └─ If all gates pass, place BUY orders for top signals
│
└─ Save state to JSON
```

### Commission & Slippage

**On every trade**:
```
Brokerage = turnover × 0.0003 (0.03%)
GST = Brokerage × 0.18
Exchange charges + SEBI = small fixed
Stamp duty = depends on side (SELL only)
STT (short-term tax) = turnover × 0.0025 (SELL only)
Slippage = entry_price × 0.0005
```

**Example Buy**:
```
Qty = 100, Price = 500
Turnover = 50,000
Brokerage = 50,000 × 0.0003 = 15
GST = 15 × 0.18 = 2.70
Total = ~18 + slippage
Actual Cost = 50,000 + 18 + slippage ≈ ₹50,020
```

### State Persistence

After every trade, state saved to `outputs/simple_paper_state.json`:
- Holds all positions, cash, realized PnL, log
- On app restart, state reloaded (trades resume from where they left off)

---

## Edge Cases & Exceptions

### 1. **Weekend/Holiday Trading**
**Issue**: App tried to trade on Saturday at opening range time.

**Solution** (fixed):
```python
def _in_entry_window() -> bool:
    now = ist_now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    return MARKET_OPEN <= t <= ENTRY_CUTOFF
```

### 2. **Stale Price Data**
**Issue**: Price data is outdated (market had moved but app used old price).

**Solution**: 
- Refresh prices from market APIs every cycle (NSE / Finnhub)
- If fetch fails, use last known price (not ideal but safe)

### 3. **Platform Timezone Issues**
**Issue**: Times were wrong if your Windows region ≠ India.

**Solution**:
```python
IST = pytz.timezone("Asia/Kolkata")
now = datetime.now(pytz.utc).astimezone(IST)
```

### 4. **Rapid Position Accumulation**
**Issue**: Could open 10+ positions if not checked.

**Solution**: `max_open_positions` slider (default 3).

### 5. **No Guards Against Profit Drawdown**
**Issue**: Won ₹6,890 but lost it all → ended at ₹3,800.

**Solution**: Profit ladder lock (above) + optional "profit guard" rule.

### 6. **Stale Zerodha Session Token**
**Issue**: Token expires at midnight; app crashes if you keep it open.

**Why not automated yet**: Zerodha requires browser 2FA (can't script easily).

**Manual fix**: Restart app after midnight.

---

## Learning Agent

### How It Works

#### 1. **Daily Trade Summary**
After market close, summarizes trades from today:
```python
{
  "date": "2026-04-25",
  "closed_trades": 5,
  "wins": 3,
  "losses": 2,
  "win_rate": 60.0,
  "net": 2500.0,
  "avg_win": 1200.0,
  "avg_loss": -400.0
}
```

#### 2. **Market Regime Detection**
Calculates:
- Average price change across signals
- Volatility (std dev of returns)
- Trend direction (up/down/flat)

```python
market_regime = "UPTREND"      # if avg pchange > 1%
volatility = "HIGH"             # if std dev > 2%
```

#### 3. **Adjustment Logic**
Based on learning memory + market regime:

```
if win_rate < 30%:
    → Raise min_buy_score (be pickier)
    
if market == "DOWNTREND":
    → Increase TP% (targets too tight in volatile market)
    
if avg_loss > avg_win:
    → Loosen SL% (exit too early on noise)
```

#### 4. **Persistence**
Learning memory saved to state file, reloaded on app restart.

---

## Setup & Usage

### Installation

```powershell
# Clone repo (if not already done)
git clone <repo>
cd StockMarket

# Create venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```powershell
# Copy template
Copy-Item config.example.json config.json

# Edit config.json with your preferences
# Key settings:
# - starting_capital: your budget
# - max_trades_per_day: how aggressive
# - stop_loss_pct & take_profit_pct: risk/reward ratio
```

### Run Live Paper Simulator (Recommended)

```powershell
# Activate venv if not active
.\.venv\Scripts\Activate.ps1

# Run unified launcher (loads dashboard_simple via app.py)
$env:PYTHONPATH = "src"
streamlit run app.py
```

**Make sure "Enable Auto Paper Trading" is checked to start auto trades**.

### Backtest Historical Data

```powershell
# Single backtest
python -m stockmarket backtest --symbol RELIANCE.NS

# Parameter sweep (find best SL%, TP%, volume threshold)
python -m stockmarket sweep --symbol RELIANCE.NS

# View latest signals
python -m stockmarket inspect --symbol RELIANCE.NS
```

### Monitoring

**Daily PnL History**: `outputs/daily_pnl_history.csv`
```csv
trade_date,realized_pnl,charges,net_pnl,trades
2026-04-21,2610.0,275.39,2334.61,8
2026-04-22,-154.8,225.75,-380.55,10
```

**All Trades**: `outputs/simple_paper_state.json` → `"log"` array

---

## File Dependencies Map

```
dashboard_simple.py (PRIMARY)
├─ imports config.py
├─ uses nsepython (NSE quotes)
├─ calls _rank_signals()
│   └─ imports strategy.py
│       └─ calls add_strategy_columns()
├─ calls _auto_paper_cycle()
│   └─ _record_trade() → _intraday_charges()
└─ saves to outputs/simple_paper_state.json

cli.py (BACKTEST)
├─ imports data.py
│   └─ calls fetch_intraday_data()
│       └─ data.fetch_intraday_data() (Finnhub US / NSE India)
├─ imports strategy.py
└─ imports backtest.py
    └─ calls run_backtest()
```

---

## Future Enhancements

### Priority 1 — Dynamic Watchlist Selection
Currently the watchlist is hardcoded to 12 stocks. Smart improvements:

1. **Momentum scanning** — scan NSE top 50 for highest price % change in last 5 min
   - Problem: Markets already moved; you'd be chasing.
   - Better: Scan for "highest volume today" (institutional accumulation signal)

2. **Volatility-based watchlist** — auto-include stocks with VIX > X
   - More moves = more opportunities
   - But also higher loss potential

3. **Sector rotation** — track which sectors are hot
   - If IT rallying, swap out finance stocks for INFY, TCS, WIPRO
   - If pharma down, add LUPIN, SUNPHARMA

4. **Correlation filtering** — avoid holding 2 highly correlated stocks
   - E.g., TATAPOWER and RELIANCE are both energy-sensitive
   - Skip one if the other is already held

### Priority 2 — Better Market Timing
- **Pre-market screening** (8 AM): scan for "most likely breakout" candidates
- **Volume surge detection**: when a stock's volume spikes 3x, flag for entry
- **Open range sizing**: adjust OR window based on first 5 min ATR (volatility)

### Priority 3 — Machine Learning
- **Historical backtesting on each stock** to rank them by Sharpe ratio
- **Only add to active watchlist** if Sharpe > 1.0 (historically profitable)
- **Retrain daily**: update rankings based on today's PnL

### Priority 4 — User Customization
- **"Edit Watchlist" UI button** in sidebar to add/remove stocks live
- **Sector/cap filters**: "Only tech stocks" or "Only ₹100–₹500" range
- **Momentum presets**: "Trending Up", "Stable", "Volatile"

---

1. **Zerodha Kite integration** — replace paper trades with real orders
2. **Configurable watchlist UI** — add/remove stocks without editing code (see Priority 4 above)
3. **Multiple strategies** — switch between ORB, moving average crossover, mean reversion, etc.
4. **Advanced risk management** — position sizing by Sharpe ratio, sector limits, VaR limits
5. **Webhook alerts** — Telegram/Discord notifications on signals
6. **Daily email reports** — PnL summary, trade analysis, next day outlook

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| App crashes on startup | Delete `simple_paper_state.json`, restart |
| No signals appear | Check watchlist symbols (correct .NS/.BO suffix), ensure market is open |
| Trades on weekends | Fixed — now blocks weekends automatically |
| "Profit ladder exited all positions" | Expected — your daily PnL hit the flag (good thing!) |
| High commissions | That's reality — Zerodha charges ~0.03% per side + GST + STT |

---

## Summary

This tool is a **complete intraday trading simulator** that lets you:
- Test strategies with paper money risk-free
- Auto-execute trades based on technical signals
- Track portfolio and PnL in real-time
- Learn from past trades
- Eventually connect to real brokers (Zerodha) for live trading

The strategy is **conservative** (ORB + VWAP + volume) and designed to catch early breakouts before the crowd. The profit ladder lock protects big wins from drawdowns.

**Good luck trading! 📈**
