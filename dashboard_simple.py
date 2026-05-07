"""
Simple Budget-Based Trading Simulator (Paper Trading)
Run with: streamlit run dashboard_simple.py --server.port 8507
"""

from __future__ import annotations
import random as _rng_mod

import json
import math
import sys
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytz
import streamlit as st

try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None

try:
    import yfinance as yf
except Exception:
    yf = None


IST = pytz.timezone("Asia/Kolkata")
US_EASTERN = pytz.timezone("America/New_York")

WATCHLIST_NSE = [
    "IEX.NS",
    "BHEL.NS",
    "IRCTC.NS",
    "FEDERALBNK.NS",
    "IDFCFIRSTB.NS",
    "AUROPHARMA.NS",
    "COFORGE.NS",
    "BSE.NS",
    "CDSL.NS",
    "TATAPOWER.NS",
    "MOTHERSON.NS",
    "PERSISTENT.NS",
    "RELIANCE.NS",
    "TCS.NS",
    "INFY.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "SBIN.NS",
    "AXISBANK.NS",
    "LT.NS",
    "ITC.NS",
    "HINDUNILVR.NS",
    "BHARTIARTL.NS",
    "TITAN.NS",
    "MARUTI.NS",
    "BAJFINANCE.NS",
    "KOTAKBANK.NS",
    "ASIANPAINT.NS",
    "ULTRACEMCO.NS",
    "HCLTECH.NS",
    "WIPRO.NS",
    "SUNPHARMA.NS",
    "NTPC.NS",
    "POWERGRID.NS",
    "ONGC.NS",
    "TATASTEEL.NS",
    "HINDALCO.NS",
    "ADANIPORTS.NS",
    "GRASIM.NS",
    "TECHM.NS",
]

WATCHLIST_US = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "NFLX",
    "AVGO",
    "JPM",
    "BAC",
    "WMT",
    "COST",
    "KO",
    "MCD",
    "CRM",
    "ADBE",
    "INTC",
    "QCOM",
]

MARKET_CONFIG = {
    "NSE": {
        "label": "India NSE",
        "timezone": IST,
        "market_open": time(9, 15),
        "entry_cutoff": time(13, 30),
        "square_off": time(15, 15),
        "watchlist": WATCHLIST_NSE,
        "state_file": Path("outputs") / "simple_paper_state.json",
        "clean_closed_trades_file": Path("outputs") / "clean_closed_trades_light.csv",
        "currency": "Rs",
        "score_change_weight": 6.0,
        "score_vwap_weight": 20.0,
        "score_range_weight": 2.0,
        "ready_pchange_threshold": 0.25,
        "ready_range_threshold": 0.5,
    },
    "US": {
        "label": "US",
        "timezone": US_EASTERN,
        "market_open": time(9, 30),
        "entry_cutoff": time(13, 30),
        "square_off": time(15, 45),
        "watchlist": WATCHLIST_US,
        "state_file": Path("outputs") / "simple_paper_state_us.json",
        "clean_closed_trades_file": Path("outputs") / "clean_closed_trades_light_us.csv",
        "currency": "$",
        "score_change_weight": 8.0,
        "score_vwap_weight": 28.0,
        "score_range_weight": 3.5,
        "ready_pchange_threshold": 0.12,
        "ready_range_threshold": 0.3,
    },
}

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Setup logging for server-side console output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger("dashboard_simple")


def _app_log(level: str, message: str) -> None:
    """Log to both server console and session state for UI display."""
    timestamp = datetime.now(IST).strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    
    # Log to server console
    if level.upper() == "INFO":
        logger.info(message)
    elif level.upper() == "WARNING":
        logger.warning(message)
    elif level.upper() == "ERROR":
        logger.error(message)
    else:
        logger.debug(message)
    
    # Add to session state for UI display
    if "s_app_logs" not in st.session_state:
        st.session_state.s_app_logs = []
    
    st.session_state.s_app_logs.append({
        "timestamp": timestamp,
        "level": level.upper(),
        "message": message,
    })
    
    # Keep only last 100 logs to prevent memory bloat
    if len(st.session_state.s_app_logs) > 100:
        st.session_state.s_app_logs = st.session_state.s_app_logs[-100:]

try:
    from stockmarket.config import TradingConfig
    from stockmarket.optimizer import collect_clean_closed_trades, export_optimization_report, run_intelligent_optimization
except Exception:
    TradingConfig = None
    collect_clean_closed_trades = None
    export_optimization_report = None
    run_intelligent_optimization = None

try:
    from stockmarket.market_learning import (
        train_market_learning_model,
        get_symbol_quality_score,
        MarketLearningModel,
        resolve_learning_model_path,
    )
except Exception:
    train_market_learning_model = None
    get_symbol_quality_score = None
    MarketLearningModel = None
    resolve_learning_model_path = None


def _selected_market() -> str:
    market = str(st.session_state.get("selected_market", "NSE")).upper()
    return market if market in MARKET_CONFIG else "NSE"


def _market_cfg() -> dict[str, Any]:
    return MARKET_CONFIG[_selected_market()]


def _state_file() -> Path:
    return Path(_market_cfg()["state_file"])


def _clean_closed_trades_file() -> Path:
    return Path(_market_cfg()["clean_closed_trades_file"])


def _watchlist() -> list[str]:
    return list(_market_cfg()["watchlist"])


def _currency_symbol() -> str:
    return str(_market_cfg()["currency"])


def _market_score_config() -> dict[str, float]:
    cfg = _market_cfg()
    return {
        "score_change_weight": float(cfg.get("score_change_weight", 6.0)),
        "score_vwap_weight": float(cfg.get("score_vwap_weight", 20.0)),
        "score_range_weight": float(cfg.get("score_range_weight", 2.0)),
        "ready_pchange_threshold": float(cfg.get("ready_pchange_threshold", 0.25)),
        "ready_range_threshold": float(cfg.get("ready_range_threshold", 0.5)),
    }


def market_now() -> datetime:
    return datetime.now(pytz.utc).astimezone(_market_cfg()["timezone"])


def _market_open_time() -> time:
    return _market_cfg()["market_open"]


def _entry_cutoff_time() -> time:
    return _market_cfg()["entry_cutoff"]


def _square_off_time() -> time:
    return _market_cfg()["square_off"]


def _read_saved_state() -> dict[str, Any]:
    state_file = _state_file()
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _auto_export_clean_closed_trades() -> tuple[int, str | None]:
    if collect_clean_closed_trades is None:
        return 0, "Optimizer module unavailable; clean closed-trade export skipped."

    # Fresh reset state can leave an empty file briefly; treat as no trades yet.
    state_file = _state_file()
    if not state_file.exists() or state_file.stat().st_size == 0:
        return 0, None

    try:
        closed_trades_df, _ = collect_clean_closed_trades(
            state_file,
            lookback_trades=0,
        )
        clean_closed_trades_file = _clean_closed_trades_file()
        clean_closed_trades_file.parent.mkdir(parents=True, exist_ok=True)
        closed_trades_df.to_csv(clean_closed_trades_file, index=False)
        return int(len(closed_trades_df)), None
    except Exception as exc:
        exc_text = str(exc)
        if (
            "Trade file is empty" in exc_text
            or "No closed trades could be derived from the provided trade file" in exc_text
        ):
            return 0, None
        return 0, f"Clean closed-trade export failed: {exc}"


def _run_optimizer_from_dashboard(
    lookback_trades: int,
    min_train_trades: int,
    quality_threshold: float,
) -> tuple[dict[str, Any] | None, dict[str, str] | None, str | None]:
    if (
        TradingConfig is None
        or run_intelligent_optimization is None
        or export_optimization_report is None
    ):
        return None, None, "Optimizer module unavailable in dashboard runtime."

    try:
        cfg_path = Path("config.json")
        cfg = TradingConfig.from_json(
            cfg_path) if cfg_path.exists() else TradingConfig()
        report = run_intelligent_optimization(
            trade_file=_state_file(),
            cfg=cfg,
            lookback_trades=int(lookback_trades),
            min_train_trades=int(min_train_trades),
            quality_threshold=float(quality_threshold),
        )
        prefix = f"optimize_dashboard_simple_{ist_now().strftime('%Y%m%d_%H%M%S')}"
        artifacts = export_optimization_report(
            report,
            out_dir="outputs",
            prefix=prefix,
        )
        artifacts_str = {k: str(v) for k, v in artifacts.items()}
        return report.summary, artifacts_str, None
    except Exception as exc:
        return None, None, f"Optimizer run failed: {exc}"


def ist_now() -> datetime:
    return datetime.now(pytz.utc).astimezone(IST)


def _auto_refresh(seconds: int) -> None:
    if seconds <= 0:
        return
    # Speed up portfolio price updates independent of full-page refresh
    _refresh_holding_prices()

    st.html(
        f"""
        <script>
            (function() {{
                const delayMs = {int(seconds) * 1000};
                setTimeout(function() {{
                    try {{
                        window.parent.postMessage({{isStreamlitMessage: true, type: 'streamlit:rerunScript'}}, '*');
                    }} catch (e) {{
                        try {{ window.location.reload(); }} catch (ee) {{}}
                    }}
                }}, delayMs);
            }})();
        </script>
        """
    )


def _quick_portfolio_metrics() -> None:
    """Render portfolio metrics with live price updates. Called on every refresh."""
    _refresh_holding_prices()

    holdings_df, unrealized, invested_capital = _portfolio_view()
    realized = float(st.session_state.s_realized)
    charges = float(st.session_state.s_charges)
    cash = float(st.session_state.s_cash)
    equity = cash + invested_capital + unrealized

    today = market_now().strftime("%Y-%m-%d")
    today_realized = sum(
        float(row.get("realized_delta", 0.0))
        for row in st.session_state.s_log
        if str(row.get("ts", "")).startswith(today)
    )
    daily_pnl = today_realized + unrealized
    daily_profit_target = float(st.session_state.get(
        "s_ui_config", {}).get("daily_profit_target", 4000.0))
    currency_symbol = _currency_symbol()

    a, b, c, d, e, f = st.columns(6)
    a.metric("Start Capital",
             f"{currency_symbol} {st.session_state.s_start:,.2f}")
    b.metric("Current Equity", f"{currency_symbol} {equity:,.2f}",
             delta=f"{currency_symbol} {(equity - st.session_state.s_start):,.2f}")
    c.metric("Cash", f"{currency_symbol} {cash:,.2f}")
    d.metric("Open PnL", f"{currency_symbol} {unrealized:,.2f}")
    e.metric("Realized PnL", f"{currency_symbol} {realized:,.2f}")
    f.metric("Total Charges", f"{currency_symbol} {charges:,.2f}",
             delta=f"Net {currency_symbol} {(realized - charges):,.2f}", delta_color="inverse")

    progress = (daily_pnl / daily_profit_target) * \
        100.0 if daily_profit_target > 0 else 0.0
    st.progress(min(1.0, max(0.0, progress / 100.0)),
                text=f"Today: {currency_symbol} {daily_pnl:,.2f} / {currency_symbol} {daily_profit_target:,.2f} ({progress:.1f}%)")


def _to_nse_symbol(symbol: str) -> str:
    return str(symbol).split(".")[0].upper()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_nse_quote(symbol: str) -> dict[str, float]:
    if nsefetch is None:
        _app_log("error", f"nsefetch library not available for {symbol}")
        raise ValueError(
            "nsepython is not installed. Run: pip install nsepython")

    nse_symbol = _to_nse_symbol(symbol)
    try:
        _app_log("info", f"Fetching NSE quote: {nse_symbol}")
        data = nsefetch(
            f"https://www.nseindia.com/api/quote-equity?symbol={nse_symbol}")
        p = data.get("priceInfo", {})

        price = float(p.get("lastPrice") or 0.0)
        vwap = float(p.get("vwap") or 0.0)
        pchange = float(p.get("pChange") or 0.0)
        ihl = p.get("intraDayHighLow", {}) or {}
        day_low = float(ihl.get("min") or 0.0)
        day_high = float(ihl.get("max") or 0.0)
        range_pct = ((day_high - day_low) / max(price, 1e-6)) * \
            100 if price > 0 else 0.0

        result = {
            "symbol": symbol,
            "price": price,
            "vwap": vwap,
            "pchange": pchange,
            "range_pct": range_pct,
        }
        _app_log("info", f"NSE {nse_symbol}: Rs {price:.2f} ({pchange:+.2f}%)")
        return result
    except Exception as e:
        if "429" in str(e) or "Rate limit" in str(e):
            _app_log("error", f"NSE API rate limit (429) for {nse_symbol}: {e}")
        elif "timeout" in str(e).lower():
            _app_log("warning", f"NSE API timeout for {nse_symbol}: {e}")
        else:
            _app_log("error", f"NSE API error for {nse_symbol}: {e}")
        raise


@st.cache_data(ttl=15, show_spinner=False)
def fetch_us_quote(symbol: str) -> dict[str, float]:
    if yf is None:
        _app_log("error", f"yfinance library not available for {symbol}")
        raise ValueError(
            "yfinance is not installed. Run: pip install yfinance")

    try:
        _app_log("info", f"Fetching US quote: {symbol}")
        ticker = yf.Ticker(str(symbol).upper())
        intraday = ticker.history(
            period="1d", interval="1m", prepost=False, auto_adjust=False)
        if intraday is None or intraday.empty:
            _app_log("warning", f"No 1m data for {symbol}, trying 5m...")
            intraday = ticker.history(
                period="5d", interval="5m", prepost=False, auto_adjust=False)
        if intraday is None or intraday.empty:
            _app_log("error", f"No intraday data available for {symbol}")
            raise ValueError(
                f"No US intraday market data for {symbol}")

        intraday = intraday.dropna(subset=["Close"]).copy()
        if intraday.empty:
            _app_log("error", f"Intraday data empty after cleanup for {symbol}")
            raise ValueError(
                f"US intraday market data empty after cleanup for {symbol}")

        latest = intraday.iloc[-1]
        price = float(latest.get("Close") or 0.0)
        high = float(intraday["High"].max() or price)
        low = float(intraday["Low"].min() or price)
        open_price = float(intraday.iloc[0].get("Open") or price)

        volume_series = intraday.get("Volume")
        if volume_series is not None and float(volume_series.fillna(0).sum()) > 0:
            close_volume = (intraday["Close"].fillna(
                0.0) * volume_series.fillna(0.0)).sum()
            total_volume = float(volume_series.fillna(0.0).sum())
            vwap = float(close_volume / max(total_volume, 1e-6))
        else:
            vwap = (high + low + open_price + price) / 4.0 if price > 0 else 0.0

        daily_hist = ticker.history(period="2d", interval="1d", auto_adjust=False)
        prev_close = 0.0
        if daily_hist is not None and not daily_hist.empty:
            daily_hist = daily_hist.dropna(subset=["Close"])
            if len(daily_hist) >= 2:
                prev_close = float(daily_hist.iloc[-2]["Close"] or 0.0)
            elif len(daily_hist) == 1:
                prev_close = float(daily_hist.iloc[-1]["Close"] or 0.0)
        pchange = ((price - prev_close) / max(prev_close, 1e-6)) * \
            100.0 if prev_close > 0 else 0.0
        range_pct = ((high - low) / max(price, 1e-6)) * 100.0 if price > 0 else 0.0

        result = {
            "symbol": symbol,
            "price": price,
            "vwap": vwap,
            "pchange": pchange,
            "range_pct": range_pct,
        }
        _app_log("info", f"US {symbol}: ${price:.2f} ({pchange:+.2f}%)")
        return result
    except Exception as e:
        if "429" in str(e) or "Rate limit" in str(e):
            _app_log("error", f"yfinance API rate limit (429) for {symbol}: {e}")
        elif "timeout" in str(e).lower() or "timed out" in str(e).lower():
            _app_log("warning", f"yfinance API timeout for {symbol}: {e}")
        elif "No data found" in str(e):
            _app_log("warning", f"No data for {symbol} (invalid ticker?): {e}")
        else:
            _app_log("error", f"yfinance API error for {symbol}: {e}")
        raise


def fetch_market_quote(symbol: str) -> dict[str, float]:
    if _selected_market() == "US":
        return fetch_us_quote(symbol)
    return fetch_nse_quote(symbol)


def _intraday_charges(side: str, turnover: float) -> float:
    brokerage = min(turnover * 0.0003, 20.0)
    exchange_txn = turnover * 0.0000325
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    stt = turnover * 0.00025 if side == "SELL" else 0.0
    return brokerage + exchange_txn + sebi + gst + stamp + stt


def _init_state(starting_capital: float) -> None:
    state_file = _state_file()
    state_key = str(state_file)
    if st.session_state.get("s_state_file") == state_key and "s_cash" in st.session_state:
        return

    for key in [
        "s_cash",
        "s_start",
        "s_realized",
        "s_charges",
        "s_holdings",
        "s_shorts",
        "s_ui_config",
        "s_log",
        "s_prices",
        "s_agent_memory",
        "s_peak_open_pnl",
        "s_peak_open_pnl_day",
        "s_profit_guard_triggered_day",
        "s_profit_ladder_day",
        "s_profit_ladder_armed",
        "s_profit_ladder_pullback_started",
        "s_profit_ladder_exited_day",
        "s_app_logs",
    ]:
        st.session_state.pop(key, None)

    if state_file.exists():
        try:
            data = _read_saved_state()
            st.session_state.s_cash = float(data.get("cash", starting_capital))
            st.session_state.s_start = float(
                data.get("start", starting_capital))
            st.session_state.s_realized = float(data.get("realized", 0.0))
            st.session_state.s_charges = float(data.get("charges", 0.0))
            st.session_state.s_holdings = data.get("holdings", {})
            st.session_state.s_shorts = data.get("shorts", {})
            st.session_state.s_ui_config = data.get("ui_config", {})
            st.session_state.s_log = data.get("log", [])
            st.session_state.s_prices = data.get("prices", {})
            st.session_state.s_agent_memory = data.get("agent_memory", {})
            st.session_state.s_peak_open_pnl = float(
                data.get("peak_open_pnl", 0.0))
            st.session_state.s_peak_open_pnl_day = str(
                data.get("peak_open_pnl_day", ""))
            st.session_state.s_profit_guard_triggered_day = str(
                data.get("profit_guard_triggered_day", "")
            )
            st.session_state.s_profit_ladder_day = str(
                data.get("profit_ladder_day", "")
            )
            st.session_state.s_profit_ladder_armed = bool(
                data.get("profit_ladder_armed", False)
            )
            st.session_state.s_profit_ladder_pullback_started = bool(
                data.get("profit_ladder_pullback_started", False)
            )
            st.session_state.s_profit_ladder_exited_day = str(
                data.get("profit_ladder_exited_day", "")
            )
            st.session_state.s_app_logs = []
            st.session_state.s_state_file = state_key
            _app_log("info", f"Loaded state for {_selected_market()} market")
            return
        except Exception as e:
            _app_log("error", f"Failed to load saved state: {e}")
            pass

    st.session_state.s_cash = float(starting_capital)
    st.session_state.s_start = float(starting_capital)
    st.session_state.s_realized = 0.0
    st.session_state.s_charges = 0.0
    st.session_state.s_holdings = {}
    st.session_state.s_shorts = {}
    st.session_state.s_ui_config = {}
    st.session_state.s_log = []
    st.session_state.s_prices = {}
    st.session_state.s_agent_memory = {}
    st.session_state.s_peak_open_pnl = 0.0
    st.session_state.s_peak_open_pnl_day = ""
    st.session_state.s_profit_guard_triggered_day = ""
    st.session_state.s_profit_ladder_day = ""
    st.session_state.s_profit_ladder_armed = False
    st.session_state.s_profit_ladder_pullback_started = False
    st.session_state.s_profit_ladder_exited_day = ""
    st.session_state.s_app_logs = []
    st.session_state.s_state_file = state_key
    _app_log("info", f"Initialized fresh state for {_selected_market()} market with {currency_symbol} {starting_capital:,.0f}")



def _save_state() -> None:
    state_file = _state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash": float(st.session_state.s_cash),
        "start": float(st.session_state.s_start),
        "realized": float(st.session_state.s_realized),
        "charges": float(st.session_state.s_charges),
        "holdings": st.session_state.s_holdings,
        "shorts": st.session_state.s_shorts,
        "ui_config": st.session_state.get("s_ui_config", {}),
        "log": st.session_state.s_log,
        "prices": st.session_state.s_prices,
        "agent_memory": st.session_state.get("s_agent_memory", {}),
        "peak_open_pnl": float(st.session_state.get("s_peak_open_pnl", 0.0)),
        "peak_open_pnl_day": st.session_state.get("s_peak_open_pnl_day", ""),
        "profit_guard_triggered_day": st.session_state.get("s_profit_guard_triggered_day", ""),
        "profit_ladder_day": st.session_state.get("s_profit_ladder_day", ""),
        "profit_ladder_armed": bool(st.session_state.get("s_profit_ladder_armed", False)),
        "profit_ladder_pullback_started": bool(st.session_state.get("s_profit_ladder_pullback_started", False)),
        "profit_ladder_exited_day": st.session_state.get("s_profit_ladder_exited_day", ""),
        "market": _selected_market(),
    }
    state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _today_entry_count() -> int:
    today = market_now().strftime("%Y-%m-%d")
    return sum(
        1
        for row in st.session_state.s_log
        if str(row.get("ts", "")).startswith(today) and str(row.get("side", "")).upper() in {"BUY", "SHORT"}
    )


def _minutes_since_last_entry(now_naive: datetime, day: str) -> float:
    for row in reversed(st.session_state.s_log):
        ts = str(row.get("ts", ""))
        if not ts.startswith(day):
            continue
        side = str(row.get("side", "")).upper()
        if side not in {"BUY", "SHORT"}:
            continue
        try:
            ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        return max(0.0, (now_naive - ts_dt).total_seconds() / 60.0)

    market_open_dt = datetime.combine(now_naive.date(), _market_open_time())
    return max(0.0, (now_naive - market_open_dt).total_seconds() / 60.0)


def _latest_exit_info_by_symbol(day: str) -> dict[str, tuple[datetime, float]]:
    latest: dict[str, tuple[datetime, float]] = {}
    for row in reversed(st.session_state.s_log):
        ts = str(row.get("ts", ""))
        if not ts.startswith(day):
            continue
        side = str(row.get("side", "")).upper()
        if side not in {"SELL", "COVER"}:
            continue
        sym = str(row.get("symbol", "")).strip()
        if not sym or sym in latest:
            continue
        try:
            ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        latest[sym] = (ts_dt, float(row.get("price", 0.0) or 0.0))
    return latest


def _latest_stop_loss_event_by_symbol() -> dict[str, tuple[datetime, str]]:
    latest: dict[str, tuple[datetime, str]] = {}
    for row in reversed(st.session_state.s_log):
        side = str(row.get("side", "")).upper()
        if side not in {"SELL", "COVER"}:
            continue
        reason = str(row.get("reason", "")).strip()
        if "sl" not in reason.lower():
            continue
        sym = str(row.get("symbol", "")).strip()
        if not sym or sym in latest:
            continue
        ts = str(row.get("ts", ""))
        try:
            ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        latest[sym] = (ts_dt, reason)
    return latest


def _current_open_pnl() -> float:
    pnl = 0.0
    for sym, h in st.session_state.s_holdings.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty > 0 and avg > 0 and ltp > 0:
            pnl += (ltp - avg) * qty
    for sym, h in st.session_state.s_shorts.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty > 0 and avg > 0 and ltp > 0:
            pnl += (avg - ltp) * qty
    return float(pnl)


def _current_gross_exposure() -> float:
    exposure = 0.0
    for sym, h in st.session_state.s_holdings.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty > 0 and ltp > 0:
            exposure += qty * ltp
    for sym, h in st.session_state.s_shorts.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty > 0 and ltp > 0:
            exposure += qty * ltp
    return float(exposure)


def _auto_filters_from_budget_slots(
    total_capital: float,
    cash_now: float,
    current_exposure: float,
    current_open_positions: int,
    max_open_positions: int,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    max_qty_per_trade: int,
    max_price: float,
) -> tuple[float, float, int]:
    remaining_slots = max(1, int(max_open_positions) -
                          int(current_open_positions))
    deploy_cap = max(0.0, float(total_capital) *
                     (float(max_total_deployment_pct) / 100.0))
    symbol_cap = max(0.0, float(total_capital) *
                     (float(max_symbol_allocation_pct) / 100.0))
    remaining_deploy = max(0.0, deploy_cap - float(current_exposure))
    qty_cap_value = max(1, int(max_qty_per_trade)) * max(1.0, float(max_price))

    per_slot_budget = min(
        max(0.0, float(cash_now)),
        max(0.0, float(remaining_deploy)),
        max(0.0, float(symbol_cap)),
        max(0.0, float(qty_cap_value)),
    ) / float(remaining_slots)

    # Keep floor practical: conservative fraction of per-slot budget to avoid choking entries.
    auto_min_order = max(100.0, per_slot_budget * 0.25)

    return float(auto_min_order), float(per_slot_budget), int(remaining_slots)


def _summarize_day_from_log(day: str) -> dict[str, Any]:
    closes = []
    reasons: dict[str, float] = {}
    for row in st.session_state.s_log:
        ts = str(row.get("ts", ""))
        side = str(row.get("side", "")).upper()
        if not ts.startswith(day):
            continue
        if side not in {"SELL", "COVER"}:
            continue
        pnl = float(row.get("realized_delta", 0.0) or 0.0)
        closes.append(pnl)
        reason = str(row.get("reason", "Unknown"))
        reasons[reason] = reasons.get(reason, 0.0) + pnl

    if not closes:
        return {
            "date": day,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "top_reason": "-",
        }

    wins = [x for x in closes if x > 0]
    losses = [x for x in closes if x <= 0]
    top_reason = max(reasons.items(), key=lambda kv: kv[1])[
        0] if reasons else "-"
    return {
        "date": day,
        "closed_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / max(len(closes), 1)) * 100.0,
        "net": float(sum(closes)),
        "avg_win": float(sum(wins) / max(len(wins), 1)) if wins else 0.0,
        "avg_loss": float(sum(losses) / max(len(losses), 1)) if losses else 0.0,
        "top_reason": top_reason,
    }


def _symbol_learning_profile() -> list[dict[str, Any]]:
    per_symbol: dict[str, list[float]] = {}
    for row in st.session_state.s_log:
        side = str(row.get("side", "")).upper()
        if side not in {"SELL", "COVER"}:
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym:
            continue
        pnl = float(row.get("realized_delta", 0.0) or 0.0)
        per_symbol.setdefault(sym, []).append(pnl)

    profile: list[dict[str, Any]] = []
    for sym, pnls in per_symbol.items():
        trades = len(pnls)
        wins = len([x for x in pnls if x > 0])
        win_rate = (wins / max(trades, 1)) * 100.0
        avg_pnl = float(sum(pnls) / max(trades, 1))
        net = float(sum(pnls))

        bias = ((win_rate - 50.0) / 50.0) * 6.0
        bias += max(-3.0, min(3.0, avg_pnl / 60.0))
        bias += max(-2.0, min(2.0, net / 500.0))
        if trades < 2:
            bias *= 0.6
        bias = float(max(-8.0, min(8.0, bias)))

        profile.append(
            {
                "symbol": sym,
                "trades": int(trades),
                "win_rate": float(win_rate),
                "avg_pnl": float(avg_pnl),
                "net": float(net),
                "bias": float(round(bias, 2)),
            }
        )

    return sorted(profile, key=lambda r: float(r.get("bias", 0.0)), reverse=True)


def _symbol_bias_map() -> dict[str, float]:
    profile = _symbol_learning_profile()
    return {
        str(r.get("symbol", "")): float(r.get("bias", 0.0) or 0.0)
        for r in profile
        if str(r.get("symbol", ""))
    }


def _update_learning_memory() -> dict[str, Any]:
    dates = sorted({str(row.get("ts", ""))[
                   :10] for row in st.session_state.s_log if str(row.get("ts", ""))})
    daily = []
    for d in dates:
        summary = _summarize_day_from_log(d)
        if int(summary.get("closed_trades", 0)) > 0:
            daily.append(summary)
    daily = daily[-30:]
    symbol_profile = _symbol_learning_profile()
    memory = {
        "daily": daily,
        "symbols": symbol_profile,
        "last_updated": market_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if st.session_state.get("s_agent_memory", {}) != memory:
        st.session_state.s_agent_memory = memory
        _save_state()
    return memory


def _market_research_from_signals(buy_df: pd.DataFrame, sell_df: pd.DataFrame) -> dict[str, Any]:
    frames = []
    if not buy_df.empty:
        frames.append(buy_df[["symbol", "pchange", "buy_signal"]].copy())
    if not sell_df.empty:
        frames.append(sell_df[["symbol", "pchange", "sell_signal"]].copy())
    if not frames:
        return {
            "regime": "unknown",
            "avg_pchange": 0.0,
            "volatility": 0.0,
            "buy_ready": 0,
            "sell_ready": 0,
        }

    merged = pd.concat(frames, axis=0, ignore_index=True).drop_duplicates(
        subset=["symbol"])
    pchange_series = pd.to_numeric(merged.get(
        "pchange"), errors="coerce").fillna(0.0)
    avg_pchange = float(pchange_series.mean())
    volatility = float(pchange_series.abs().mean())
    buy_ready = int((buy_df["buy_signal"] == "READY").sum()) if (
        not buy_df.empty and "buy_signal" in buy_df.columns) else 0
    sell_ready = int((sell_df["sell_signal"] == "READY").sum()) if (
        not sell_df.empty and "sell_signal" in sell_df.columns) else 0

    regime = "mixed"
    if avg_pchange >= 0.4 and buy_ready >= sell_ready:
        regime = "bullish"
    elif avg_pchange <= -0.4 and sell_ready >= buy_ready:
        regime = "bearish"
    elif abs(avg_pchange) < 0.25:
        regime = "sideways"

    return {
        "regime": regime,
        "avg_pchange": avg_pchange,
        "volatility": volatility,
        "buy_ready": buy_ready,
        "sell_ready": sell_ready,
    }


def _best_ai_action(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    market_research: dict[str, Any],
    min_buy_score: float,
    min_short_score: float,
    min_sell_score: float,
    enable_short_selling: bool,
    enable_regime_entry_gate: bool,
) -> dict[str, Any]:
    regime = str(market_research.get("regime", "unknown")).strip().lower()
    allow_buy = (not enable_regime_entry_gate) or regime in {
        "bullish", "mixed", "unknown"}
    allow_short = (not enable_regime_entry_gate) or regime in {
        "bearish", "mixed", "unknown"}

    candidates: list[dict[str, Any]] = []

    # Prioritize valid long exits if risk-off signal is active on existing holdings.
    if not sell_exit_df.empty:
        for _, row in sell_exit_df.iterrows():
            sym = str(row.get("symbol", ""))
            if sym not in st.session_state.get("s_holdings", {}):
                continue
            if str(row.get("sell_signal", "WAIT")) != "READY":
                continue
            score = float(row.get("effective_sell_score",
                          row.get("sell_score", 0.0)) or 0.0)
            if score < float(min_sell_score):
                continue
            candidates.append(
                {
                    "action": "EXIT LONG",
                    "symbol": sym,
                    "score": score,
                    "confidence": min(100.0, score + 8.0),
                    "reason": f"Sell-exit signal READY with score {score:.1f}",
                }
            )

    if not buy_df.empty and allow_buy:
        for _, row in buy_df.iterrows():
            if str(row.get("buy_signal", "WAIT")) != "READY":
                continue
            sym = str(row.get("symbol", ""))
            if sym in st.session_state.get("s_holdings", {}) or sym in st.session_state.get("s_shorts", {}):
                continue
            score = float(row.get("effective_buy_score",
                          row.get("buy_score", 0.0)) or 0.0)
            if score < float(min_buy_score):
                continue
            tag = str(row.get("research_tag", ""))
            bonus = 4.0 if tag == "Healthy setup" else (
                -6.0 if tag == "Overextended" else -2.0)
            candidates.append(
                {
                    "action": "BUY",
                    "symbol": sym,
                    "score": score + bonus,
                    "confidence": max(0.0, min(100.0, score + bonus)),
                    "reason": f"Buy signal READY, setup={tag or 'n/a'}, score {score:.1f}",
                }
            )

    if enable_short_selling and (not sell_df.empty) and allow_short:
        for _, row in sell_df.iterrows():
            if str(row.get("sell_signal", "WAIT")) != "READY":
                continue
            sym = str(row.get("symbol", ""))
            if sym in st.session_state.get("s_holdings", {}) or sym in st.session_state.get("s_shorts", {}):
                continue
            score = float(row.get("effective_sell_score",
                          row.get("sell_score", 0.0)) or 0.0)
            if score < float(min_short_score):
                continue
            tag = str(row.get("research_tag", ""))
            bonus = 4.0 if tag == "Healthy setup" else (
                -6.0 if tag == "Overextended" else -2.0)
            candidates.append(
                {
                    "action": "SHORT",
                    "symbol": sym,
                    "score": score + bonus,
                    "confidence": max(0.0, min(100.0, score + bonus)),
                    "reason": f"Short signal READY, setup={tag or 'n/a'}, score {score:.1f}",
                }
            )

    if not candidates:
        gate_note = ""
        if enable_regime_entry_gate:
            if (not allow_buy) and (not allow_short):
                gate_note = f" Regime gate active ({regime})."
            elif not allow_buy:
                gate_note = f" Buy blocked by regime ({regime})."
            elif not allow_short:
                gate_note = f" Short blocked by regime ({regime})."
        return {
            "action": "HOLD",
            "symbol": "-",
            "confidence": 0.0,
            "reason": f"No qualified signal above thresholds.{gate_note}",
        }

    best = max(candidates, key=lambda x: float(x.get("score", 0.0)))
    return {
        "action": str(best.get("action", "HOLD")),
        "symbol": str(best.get("symbol", "-")),
        "confidence": float(best.get("confidence", 0.0)),
        "reason": str(best.get("reason", "")),
    }


def _agent_tuning_plan(
    base_min_buy_score: float,
    base_min_short_score: float,
    base_tp_pct: float,
    base_idle_buy_fallback_minutes: int,
    base_reentry_cooldown_minutes: int,
    learning_memory: dict[str, Any],
    market_research: dict[str, Any],
) -> dict[str, Any]:
    max_practical_min_score = 85.0
    adj_buy = 0.0
    adj_short = 0.0
    tp_mult = 1.0
    suggested_idle_fallback = int(base_idle_buy_fallback_minutes)
    suggested_reentry_cooldown = int(base_reentry_cooldown_minutes)
    notes: list[str] = []

    daily = learning_memory.get("daily", []) if isinstance(
        learning_memory, dict) else []
    latest = daily[-1] if daily else None
    if isinstance(latest, dict):
        closed_trades = int(latest.get("closed_trades", 0))
        win_rate = float(latest.get("win_rate", 0.0))
        net = float(latest.get("net", 0.0))
        if closed_trades >= 3:
            if net < 0 or win_rate < 45.0:
                adj_buy += 3.0
                adj_short += 3.0
                tp_mult *= 0.9
                suggested_idle_fallback = min(
                    240, suggested_idle_fallback + 10)
                suggested_reentry_cooldown = min(
                    120, suggested_reentry_cooldown + 5)
                notes.append(
                    "Yesterday weak: tightened entries and quicker profit booking.")
            elif net > 0 and win_rate >= 60.0:
                adj_buy -= 1.0
                adj_short -= 1.0
                tp_mult *= 1.05
                suggested_idle_fallback = max(5, suggested_idle_fallback - 5)
                suggested_reentry_cooldown = max(
                    0, suggested_reentry_cooldown - 2)
                notes.append("Yesterday strong: slightly relaxed entries.")

    regime = str(market_research.get("regime", "mixed"))
    if regime == "bearish":
        adj_buy += 2.0
        adj_short -= 1.0
        notes.append("Market bearish: stricter longs, easier shorts.")
    elif regime == "bullish":
        adj_buy -= 1.0
        adj_short += 2.0
        notes.append("Market bullish: easier longs, stricter shorts.")
    elif regime == "sideways":
        tp_mult *= 0.92
        suggested_idle_fallback = min(240, suggested_idle_fallback + 5)
        notes.append("Sideways regime: faster profit booking.")

    return {
        "effective_min_buy_score": float(min(max_practical_min_score, max(0.0, base_min_buy_score + adj_buy))),
        "effective_min_short_score": float(min(max_practical_min_score, max(0.0, base_min_short_score + adj_short))),
        "effective_tp_pct": float(min(0.06, max(0.003, base_tp_pct * tp_mult))),
        "suggested_idle_buy_fallback_minutes": int(suggested_idle_fallback),
        "suggested_reentry_cooldown_minutes": int(suggested_reentry_cooldown),
        "notes": notes,
        "latest_learning": latest,
    }


def _max_feasible_order_value(
    total_capital: float,
    cash_now: float,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    max_qty_per_trade: int,
    max_price: float,
) -> float:
    symbol_cap = max(0.0, float(total_capital) *
                     (float(max_symbol_allocation_pct) / 100.0))
    deploy_cap = max(0.0, float(total_capital) *
                     (float(max_total_deployment_pct) / 100.0))
    qty_cap_value = max(1, int(max_qty_per_trade)) * max(1.0, float(max_price))
    cash_cap = max(0.0, float(cash_now))
    return float(max(100.0, min(symbol_cap, deploy_cap, qty_cap_value, cash_cap)))


def _hhmm_to_time(hhmm: int) -> time:
    val = int(max(0, min(2359, hhmm)))
    h = max(0, min(23, val // 100))
    m = max(0, min(59, val % 100))
    return time(h, m)


def _record_trade(
    symbol: str,
    side: str,
    qty: int,
    price: float,
    reason: str,
    sl_pct: float | None = None,
    tp_pct: float | None = None,
) -> None:
    value = float(qty) * float(price)
    ch = _intraday_charges(side, value)
    realized_delta = 0.0

    if side == "BUY":
        st.session_state.s_cash -= (value + ch)
        h = st.session_state.s_holdings.get(symbol, {"qty": 0, "avg": 0.0})
        old_qty = int(h.get("qty", 0))
        old_avg = float(h.get("avg", 0.0))
        new_qty = old_qty + int(qty)
        new_avg = ((old_qty * old_avg) + value) / max(new_qty, 1)
        stop_price = new_avg * \
            (1.0 - float(sl_pct if sl_pct is not None else 0.008))
        target_price = new_avg * \
            (1.0 + float(tp_pct if tp_pct is not None else 0.016))
        st.session_state.s_holdings[symbol] = {
            "qty": new_qty,
            "avg": new_avg,
            "stop": stop_price,
            "target": target_price,
        }
        _app_log("info", f"BUY {symbol}: {qty}@Rs{price:.2f} | Avg: Rs{new_avg:.2f} | {reason}")
    elif side == "SELL":
        h = st.session_state.s_holdings.get(symbol, {"qty": 0, "avg": 0.0})
        old_qty = int(h.get("qty", 0))
        old_avg = float(h.get("avg", 0.0))
        if old_qty <= 0:
            return
        exit_qty = min(old_qty, int(qty))
        pnl = (float(price) - old_avg) * float(exit_qty)
        st.session_state.s_realized += pnl
        realized_delta = float(pnl)
        st.session_state.s_cash += (float(exit_qty) * float(price) - ch)
        remain = old_qty - exit_qty
        if remain > 0:
            st.session_state.s_holdings[symbol] = {
                "qty": remain,
                "avg": old_avg,
                "stop": float(h.get("stop", old_avg * 0.992)),
                "target": float(h.get("target", old_avg * 1.016)),
            }
        else:
            st.session_state.s_holdings.pop(symbol, None)
        _app_log("info", f"SELL {symbol}: {exit_qty}@Rs{price:.2f} | PnL: Rs{pnl:.2f} | {reason}")
    elif side == "SHORT":
        st.session_state.s_cash += (value - ch)
        h = st.session_state.s_shorts.get(symbol, {"qty": 0, "avg": 0.0})
        old_qty = int(h.get("qty", 0))
        old_avg = float(h.get("avg", 0.0))
        new_qty = old_qty + int(qty)
        new_avg = ((old_qty * old_avg) + value) / max(new_qty, 1)
        stop_price = new_avg * \
            (1.0 + float(sl_pct if sl_pct is not None else 0.008))
        target_price = new_avg * \
            (1.0 - float(tp_pct if tp_pct is not None else 0.016))
        st.session_state.s_shorts[symbol] = {
            "qty": new_qty,
            "avg": new_avg,
            "stop": stop_price,
            "target": target_price,
        }
        _app_log("info", f"SHORT {symbol}: {qty}@Rs{price:.2f} | Avg: Rs{new_avg:.2f} | {reason}")
    elif side == "COVER":
        h = st.session_state.s_shorts.get(symbol, {"qty": 0, "avg": 0.0})
        old_qty = int(h.get("qty", 0))
        old_avg = float(h.get("avg", 0.0))
        if old_qty <= 0:
            return
        cover_qty = min(old_qty, int(qty))
        pnl = (old_avg - float(price)) * float(cover_qty)
        st.session_state.s_realized += pnl
        realized_delta = float(pnl)
        st.session_state.s_cash -= (float(cover_qty) * float(price) + ch)
        remain = old_qty - cover_qty
        if remain > 0:
            st.session_state.s_shorts[symbol] = {
                "qty": remain,
                "avg": old_avg,
                "stop": float(h.get("stop", old_avg * 1.008)),
                "target": float(h.get("target", old_avg * 0.984)),
            }
        else:
            st.session_state.s_shorts.pop(symbol, None)
        _app_log("info", f"COVER {symbol}: {cover_qty}@Rs{price:.2f} | PnL: Rs{pnl:.2f} | {reason}")
    else:
        return

    st.session_state.s_charges += ch
    st.session_state.s_prices[symbol] = float(price)
    st.session_state.s_log.append(
        {
            "ts": market_now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "qty": int(qty),
            "price": float(price),
            "charges": float(ch),
            "realized_delta": float(realized_delta),
            "reason": reason,
            "cash_after": float(st.session_state.s_cash),
        }
    )
    _save_state()


def _top_up_small_holdings(target_qty: int, sl_pct: float, tp_pct: float, ignore_cash_check: bool = False) -> list[str]:
    actions: list[str] = []
    target = int(max(1, target_qty))

    for sym, h in list(st.session_state.s_holdings.items()):
        curr_qty = int(h.get("qty", 0))
        if curr_qty <= 0 or curr_qty >= target:
            continue

        add_qty = target - curr_qty
        ltp = float(st.session_state.s_prices.get(
            sym, h.get("avg", 0.0) or 0.0))
        if ltp <= 0:
            actions.append(f"TOP-UP {sym} SKIPPED: no valid price")
            continue

        est_value = float(add_qty) * float(ltp)
        est_ch = _intraday_charges("BUY", est_value)
        if (not ignore_cash_check) and float(st.session_state.s_cash) < (est_value + est_ch):
            actions.append(f"TOP-UP {sym} SKIPPED: insufficient cash")
            continue

        _record_trade(
            sym,
            "BUY",
            add_qty,
            ltp,
            f"Manual top-up to qty {target}",
            sl_pct=sl_pct,
            tp_pct=tp_pct,
        )
        if ignore_cash_check:
            actions.append(
                f"TOP-UP {sym}: cash check ignored for testing")
        actions.append(
            f"TOP-UP {sym}: +{add_qty} qty @ Rs {ltp:.2f} (now {target})")

    return actions


def _in_entry_window() -> bool:
    now = market_now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    return _market_open_time() <= t <= _entry_cutoff_time()


@st.cache_data(ttl=5, show_spinner=False)
def _scan_watchlist(
    symbols: tuple[str, ...],
    min_price: float,
    max_price: float,
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    score_cfg = _market_score_config()

    for sym in symbols:
        try:
            q = fetch_market_quote(sym)
            price = float(q["price"])
            if price <= 0:
                continue
            if price < float(min_price) or price > float(max_price):
                continue

            vwap = float(q["vwap"])
            pchange = float(q["pchange"])
            range_pct = float(q["range_pct"])

            buy_score = 0.0
            sell_score = 0.0
            buy_ready = False
            sell_ready = False

            if vwap > 0:
                above = max(0.0, (price - vwap) / vwap * 100.0)
                below = max(0.0, (vwap - price) / vwap * 100.0)
            else:
                above = 0.0
                below = 0.0

            change_weight = float(score_cfg["score_change_weight"])
            vwap_weight = float(score_cfg["score_vwap_weight"])
            range_weight = float(score_cfg["score_range_weight"])
            ready_pchange_threshold = float(
                score_cfg["ready_pchange_threshold"])
            ready_range_threshold = float(score_cfg["ready_range_threshold"])

            buy_score += min(45.0, max(0.0, pchange) * change_weight)
            buy_score += min(35.0, above * vwap_weight)
            buy_score += min(20.0, range_pct * range_weight)
            buy_ready = price > vwap and pchange > ready_pchange_threshold and range_pct > ready_range_threshold

            sell_score += min(45.0, max(0.0, -pchange) * change_weight)
            sell_score += min(35.0, below * vwap_weight)
            sell_score += min(20.0, range_pct * range_weight)
            sell_ready = price < vwap and pchange < - \
                ready_pchange_threshold and range_pct > ready_range_threshold

            rows.append(
                {
                    "symbol": sym,
                    "price": round(price, 2),
                    "buy_score": round(buy_score, 2),
                    "buy_signal": "READY" if buy_ready else "WAIT",
                    "sell_score": round(sell_score, 2),
                    "sell_signal": "READY" if sell_ready else "WAIT",
                    "pchange": round(pchange, 2),
                    "range_pct": round(range_pct, 2),
                    "vwap_gap_pct": round((price - vwap) / vwap * 100.0, 2) if vwap > 0 else 0.0,
                    "updated": market_now().strftime("%H:%M:%S"),
                }
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")

    return pd.DataFrame(rows), errors


@st.cache_data(ttl=120, show_spinner=False)
def _batch_ml_scores_cached(
    symbols: tuple[str, ...],
    state_mtime: float,
    model_mtime: float,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    if get_symbol_quality_score is None:
        return scores
    state_file = _state_file()
    for sym in symbols:
        try:
            scores[sym] = float(get_symbol_quality_score(sym, state_file))
        except Exception:
            continue
    return scores


def _apply_effective_scores(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    bias_map: dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ml_enabled = bool(
        st.session_state.get("s_ui_config", {}).get("enable_ml_scoring", False)
    )

    buy_out = buy_df.copy() if not buy_df.empty else buy_df
    sell_out = sell_df.copy() if not sell_df.empty else sell_df
    sell_exit_out = sell_exit_df.copy() if not sell_exit_df.empty else sell_exit_df

    if not buy_out.empty:
        buy_out["symbol_bias"] = buy_out["symbol"].map(
            lambda s: float(bias_map.get(str(s), 0.0))
        )
    if not sell_out.empty:
        sell_out["symbol_bias"] = sell_out["symbol"].map(
            lambda s: float(bias_map.get(str(s), 0.0))
        )
    if not sell_exit_out.empty:
        sell_exit_out["symbol_bias"] = sell_exit_out["symbol"].map(
            lambda s: float(bias_map.get(str(s), 0.0))
        )

    if not buy_out.empty:
        rule_buy_score = pd.to_numeric(
            buy_out["buy_score"], errors="coerce").fillna(0.0)
        buy_bias = pd.to_numeric(
            buy_out["symbol_bias"], errors="coerce").fillna(0.0)

        if ml_enabled and get_symbol_quality_score is not None:
            state_file = _state_file()
            model_file = resolve_learning_model_path(
                state_file) if resolve_learning_model_path is not None else Path("outputs") / "market_learning_model.pkl"
            state_mtime = float(
                state_file.stat().st_mtime) if state_file.exists() else 0.0
            model_mtime = float(
                model_file.stat().st_mtime) if model_file.exists() else 0.0

            buy_symbols = tuple(
                sorted({str(s) for s in buy_out["symbol"].astype(str).tolist()}))
            ml_scores = _batch_ml_scores_cached(
                buy_symbols, state_mtime, model_mtime)
            buy_out["ml_quality_score"] = buy_out["symbol"].map(
                lambda s: float(ml_scores.get(str(s), 0.5)))
            ml_score = pd.to_numeric(
                buy_out["ml_quality_score"], errors="coerce").fillna(0.5)

            # Blend heuristic intraday signal with learned quality to keep scores data-driven.
            blended_buy_score = (0.65 * rule_buy_score) + \
                (0.35 * (ml_score * 100.0))
            cap_series = pd.Series(92.0, index=blended_buy_score.index)
            cap_series = cap_series.where(
                ~((rule_buy_score >= 90.0) & (ml_score >= 0.80)),
                97.0,
            )
            effective_buy_score = (blended_buy_score +
                                   (0.8 * buy_bias)).clip(lower=0.0)
            effective_buy_score = effective_buy_score.where(
                effective_buy_score <= cap_series, cap_series)
            buy_out["effective_buy_score"] = effective_buy_score.round(2)
        else:
            buy_out["effective_buy_score"] = (
                rule_buy_score + buy_bias
            ).clip(lower=0.0, upper=95.0).round(2)

    if not sell_out.empty:
        sell_out["effective_sell_score"] = (
            pd.to_numeric(sell_out["sell_score"], errors="coerce").fillna(0.0)
            + pd.to_numeric(sell_out["symbol_bias"],
                            errors="coerce").fillna(0.0)
        ).clip(lower=0.0, upper=95.0).round(2)

    if not sell_exit_out.empty:
        sell_exit_out["effective_sell_score"] = (
            pd.to_numeric(sell_exit_out["sell_score"],
                          errors="coerce").fillna(0.0)
            + pd.to_numeric(sell_exit_out["symbol_bias"],
                            errors="coerce").fillna(0.0)
        ).clip(lower=0.0, upper=95.0).round(2)

    return buy_out, sell_out, sell_exit_out


def _rank_signals(
    symbols: list[str],
    min_price: float,
    max_price: float,
    risk_pct: float,
    sl_pct: float,
    tp_pct: float,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    max_qty_per_trade: int,
    max_open_positions: int,
    min_order_value: float,
    max_trade_invest_pct: float = 10.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    total_capital = float(st.session_state.get("s_start", 0.0) or 0.0)
    cash_now = float(st.session_state.get("s_cash", 0.0) or 0.0)
    risk_budget = total_capital * (float(risk_pct) / 100.0)
    symbol_cap_value = total_capital * \
        (float(max_symbol_allocation_pct) / 100.0)
    deploy_cap_value = total_capital * \
        (float(max_total_deployment_pct) / 100.0)
    max_qty_limit = max(1, int(max_qty_per_trade))
    max_invest_per_trade = total_capital * \
        (float(max_total_deployment_pct) / 100.0) * \
        (float(max_trade_invest_pct) / 100.0)
    open_positions_now = len(st.session_state.get("s_holdings", {})) + len(
        st.session_state.get("s_shorts", {}))
    remaining_slots_base = max(
        1, int(max_open_positions) - int(open_positions_now))

    symbol_exposure: dict[str, float] = {}
    total_exposure = 0.0
    for sym, h in st.session_state.get("s_holdings", {}).items():
        qty = int(h.get("qty", 0))
        if qty <= 0:
            continue
        ltp = float(st.session_state.get("s_prices", {}).get(
            sym, h.get("avg", 0.0) or 0.0))
        if ltp <= 0:
            continue
        val = float(qty) * float(ltp)
        symbol_exposure[sym] = symbol_exposure.get(sym, 0.0) + val
        total_exposure += val

    for sym, h in st.session_state.get("s_shorts", {}).items():
        qty = int(h.get("qty", 0))
        if qty <= 0:
            continue
        ltp = float(st.session_state.get("s_prices", {}).get(
            sym, h.get("avg", 0.0) or 0.0))
        if ltp <= 0:
            continue
        val = float(qty) * float(ltp)
        symbol_exposure[sym] = symbol_exposure.get(sym, 0.0) + val
        total_exposure += val

    all_df, errors = _scan_watchlist(
        tuple(symbols), float(min_price), float(max_price))
    if all_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), errors

    # Research layer: prefer setups with healthy momentum and avoid overextended moves.
    all_df = all_df.copy()
    pchange_series = pd.to_numeric(all_df.get(
        "pchange"), errors="coerce").fillna(0.0)
    range_series = pd.to_numeric(all_df.get(
        "range_pct"), errors="coerce").fillna(0.0)
    vwap_gap_series = pd.to_numeric(all_df.get(
        "vwap_gap_pct"), errors="coerce").fillna(0.0)

    stretch_penalty = (vwap_gap_series.abs() - 1.8).clip(lower=0.0) * 6.0
    range_penalty = (range_series - 3.5).clip(lower=0.0) * 4.0
    range_bonus = range_series.clip(lower=0.0, upper=2.5) * 3.0

    buy_research_score = (
        pd.to_numeric(all_df.get("buy_score"), errors="coerce").fillna(0.0)
        + range_bonus
        + (pchange_series.clip(lower=0.0, upper=2.0) * 3.0)
        - stretch_penalty
        - range_penalty
    ).clip(lower=0.0, upper=100.0)

    sell_research_score = (
        pd.to_numeric(all_df.get("sell_score"), errors="coerce").fillna(0.0)
        + range_bonus
        + ((-pchange_series).clip(lower=0.0, upper=2.0) * 3.0)
        - stretch_penalty
        - range_penalty
    ).clip(lower=0.0, upper=100.0)

    all_df["research_buy_score"] = buy_research_score.round(2)
    all_df["research_sell_score"] = sell_research_score.round(2)
    all_df["research_tag"] = pd.Series(
        "Healthy setup", index=all_df.index, dtype="object"
    )
    all_df["research_tag"] = all_df["research_tag"].where(
        range_series >= 0.4, "Weak momentum"
    )
    all_df["research_tag"] = all_df["research_tag"].where(
        stretch_penalty < 6.0, "Overextended"
    )

    # Unfiltered sell shortlist is reserved for long-exit signal handling.
    sell_exit_df = all_df.sort_values(["research_sell_score", "sell_score", "pchange"], ascending=[
        False, False, True]).head(5).reset_index(drop=True)

    buy_candidates = all_df.sort_values(["research_buy_score", "buy_score", "pchange"], ascending=[
                                        False, False, False]).reset_index(drop=True)
    buy_rows: list[dict[str, Any]] = []
    buy_symbol_exposure = dict(symbol_exposure)
    buy_total_exposure = float(total_exposure)
    buy_cash_left = float(cash_now)
    for _, row in buy_candidates.iterrows():
        sym = str(row.get("symbol", ""))
        price = float(row.get("price", 0.0) or 0.0)
        if price <= 0:
            continue

        risk_per_share = max(price * float(sl_pct), 0.01)
        qty_by_risk = int(risk_budget // risk_per_share)
        qty_by_cash = int(max(0.0, buy_cash_left) // max(price, 1e-6))
        remaining_symbol_cap = max(
            0.0, symbol_cap_value - buy_symbol_exposure.get(sym, 0.0))
        qty_by_symbol_cap = int(remaining_symbol_cap // max(price, 1e-6))
        remaining_total_cap = max(0.0, deploy_cap_value - buy_total_exposure)
        qty_by_total_cap = int(remaining_total_cap // max(price, 1e-6))

        remaining_slots = max(1, remaining_slots_base - len(buy_rows))
        slot_budget = min(
            max(0.0, buy_cash_left),
            max(0.0, remaining_total_cap),
        ) / float(remaining_slots)
        qty_by_slot_budget = int(max(0.0, slot_budget) // max(price, 1e-6))

        qty_by_trade_cap = int(max_invest_per_trade // max(price, 1e-6))
        qty_upper = max(0, min(
            qty_by_risk,
            qty_by_cash,
            qty_by_symbol_cap,
            qty_by_total_cap,
            qty_by_slot_budget,
            qty_by_trade_cap,
            max_qty_limit,
        ))

        effective_min_order_value = min(
            float(min_order_value),
            max(100.0, float(slot_budget)),
            max(100.0, float(remaining_symbol_cap)),
            max(100.0, float(remaining_total_cap)),
        )
        qty_floor_by_value = max(
            1, int(math.ceil(float(effective_min_order_value) / max(price, 1e-6))))

        qty_lower = qty_floor_by_value
        if qty_upper <= 0 or qty_upper < qty_lower:
            continue

        est_value = float(qty_upper) * float(price)
        est_ch = _intraday_charges("BUY", est_value)
        if buy_cash_left < (est_value + est_ch):
            continue
        expected_profit = float(est_value) * float(tp_pct)

        rec = row.to_dict()
        rec["rank_qty"] = int(qty_upper)
        rec["rank_order_value"] = float(est_value)
        rec["rank_expected_profit"] = float(expected_profit)
        rec["rank_symbol_headroom"] = float(remaining_symbol_cap)
        rec["rank_deployment_headroom"] = float(remaining_total_cap)
        buy_rows.append(rec)

        buy_cash_left -= (est_value + est_ch)
        buy_symbol_exposure[sym] = buy_symbol_exposure.get(
            sym, 0.0) + est_value
        buy_total_exposure += est_value
        if len(buy_rows) >= 5:
            break

    sell_candidates = all_df.sort_values(["research_sell_score", "sell_score", "pchange"], ascending=[
                                         False, False, True]).reset_index(drop=True)
    sell_rows: list[dict[str, Any]] = []
    short_symbol_exposure = dict(symbol_exposure)
    short_total_exposure = float(total_exposure)
    short_cash_left = float(cash_now)
    for _, row in sell_candidates.iterrows():
        sym = str(row.get("symbol", ""))
        price = float(row.get("price", 0.0) or 0.0)
        if price <= 0:
            continue

        risk_per_share = max(price * float(sl_pct), 0.01)
        qty_by_risk = int(risk_budget // risk_per_share)
        qty_by_margin = int(max(0.0, short_cash_left) //
                            max(price * 0.2, 0.01))
        remaining_symbol_cap = max(
            0.0, symbol_cap_value - short_symbol_exposure.get(sym, 0.0))
        qty_by_symbol_cap = int(remaining_symbol_cap // max(price, 1e-6))
        remaining_total_cap = max(0.0, deploy_cap_value - short_total_exposure)
        qty_by_total_cap = int(remaining_total_cap // max(price, 1e-6))

        remaining_slots = max(1, remaining_slots_base - len(sell_rows))
        slot_budget = min(
            max(0.0, short_cash_left),
            max(0.0, remaining_total_cap),
        ) / float(remaining_slots)
        qty_by_slot_budget = int(max(0.0, slot_budget) // max(price, 1e-6))

        qty_by_trade_cap = int(max_invest_per_trade // max(price, 1e-6))
        qty_upper = max(0, min(
            qty_by_risk,
            qty_by_margin,
            qty_by_symbol_cap,
            qty_by_total_cap,
            qty_by_slot_budget,
            qty_by_trade_cap,
            max_qty_limit,
        ))

        effective_min_order_value = min(
            float(min_order_value),
            max(100.0, float(slot_budget)),
            max(100.0, float(remaining_symbol_cap)),
            max(100.0, float(remaining_total_cap)),
        )
        qty_floor_by_value = max(
            1, int(math.ceil(float(effective_min_order_value) / max(price, 1e-6))))
        if qty_upper <= 0 or qty_upper < qty_floor_by_value:
            continue

        est_value = float(qty_upper) * float(price)
        est_ch = _intraday_charges("SELL", est_value)
        est_margin = est_value * 0.2
        if short_cash_left < (est_margin + est_ch):
            continue

        rec = row.to_dict()
        rec["rank_qty"] = int(qty_upper)
        rec["rank_order_value"] = float(est_value)
        rec["rank_expected_profit"] = float(est_value * float(tp_pct))
        rec["rank_symbol_headroom"] = float(remaining_symbol_cap)
        rec["rank_deployment_headroom"] = float(remaining_total_cap)
        sell_rows.append(rec)

        short_cash_left -= (est_margin + est_ch)
        short_symbol_exposure[sym] = short_symbol_exposure.get(
            sym, 0.0) + est_value
        short_total_exposure += est_value
        if len(sell_rows) >= 5:
            break

    buy_df = pd.DataFrame(buy_rows)
    sell_df = pd.DataFrame(sell_rows)
    return buy_df, sell_df, sell_exit_df, errors


def _refresh_holding_prices() -> None:
    symbols = set(st.session_state.s_holdings.keys()) | set(
        st.session_state.s_shorts.keys())
    for sym in list(symbols):
        try:
            q = fetch_market_quote(sym)
            p = float(q.get("price") or 0.0)
            if p > 0:
                st.session_state.s_prices[sym] = p
        except Exception:
            continue


def _auto_paper_cycle(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    risk_pct: float,
    max_trades_day: int,
    max_positions: int,
    sl_pct: float,
    tp_pct: float,
    min_buy_score: float,
    enable_signal_sell: bool,
    min_sell_score: float,
    max_signal_exits_per_cycle: int,
    enable_short_selling: bool,
    min_short_score: float,
    enable_profit_guard: bool,
    profit_guard_drawdown_pct: float,
    profit_guard_after: time,
    block_new_entries_on_guard: bool,
    daily_profit_target: float,
    reentry_cooldown_minutes: int,
    reentry_min_move_pct: float,
    enable_regime_entry_gate: bool,
    market_regime: str,
    sl_cooldown_after_stop_minutes: int,
    max_qty_per_trade: int,
    symbols: list[str],
    min_price: float,
    max_price: float,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    min_order_value: float,
    idle_buy_fallback_minutes: int,
    max_trade_invest_pct: float = 10.0,
) -> list[str]:
    actions: list[str] = []

    # Keep portfolio LTP moving every cycle, even when there are no fresh signals.
    _refresh_holding_prices()

    if buy_df.empty and sell_df.empty and sell_exit_df.empty:
        return actions

    now = market_now()
    now_naive = now.replace(tzinfo=None)
    today = now.strftime("%Y-%m-%d")
    same_cycle_exited_symbols: set[str] = set()
    same_cycle_entered_symbols: set[str] = set()

    # Block all automated actions on weekends
    if now.weekday() >= 5:
        return ["Market closed (weekend) — no automated actions"]

    if st.session_state.get("s_peak_open_pnl_day", "") != today:
        st.session_state.s_peak_open_pnl_day = today
        st.session_state.s_peak_open_pnl = 0.0
        st.session_state.s_profit_guard_triggered_day = ""

    if st.session_state.get("s_profit_ladder_day", "") != today:
        st.session_state.s_profit_ladder_day = today
        st.session_state.s_profit_ladder_armed = False
        st.session_state.s_profit_ladder_pullback_started = False
        st.session_state.s_profit_ladder_exited_day = ""

    current_open_pnl = _current_open_pnl()
    st.session_state.s_peak_open_pnl = max(
        float(st.session_state.get("s_peak_open_pnl", 0.0)),
        float(current_open_pnl),
    )

    today_realized = sum(
        float(row.get("realized_delta", 0.0) or 0.0)
        for row in st.session_state.s_log
        if str(row.get("ts", "")).startswith(today)
    )
    daily_pnl_now = float(today_realized + current_open_pnl)

    ladder_target = float(daily_profit_target)
    ladder_high = ladder_target + 2000.0
    ladder_floor = ladder_target + 1000.0

    if (
        ladder_target > 0.0
        and st.session_state.get("s_profit_ladder_exited_day", "") != today
    ):
        if (not st.session_state.get("s_profit_ladder_armed", False)) and daily_pnl_now >= ladder_target:
            st.session_state.s_profit_ladder_armed = True
            st.session_state.s_profit_ladder_pullback_started = False
            actions.append(
                f"Profit ladder armed at Rs {ladder_target:,.0f}; aiming Rs {ladder_high:,.0f}, lock Rs {ladder_floor:,.0f}"
            )

        if st.session_state.get("s_profit_ladder_armed", False):
            should_exit_ladder = False
            ladder_reason = ""

            if daily_pnl_now >= ladder_high:
                should_exit_ladder = True
                ladder_reason = f"Profit ladder target hit @ Rs {daily_pnl_now:,.2f}"
            else:
                if daily_pnl_now < (ladder_high - 1.0):
                    st.session_state.s_profit_ladder_pullback_started = True
                if st.session_state.get("s_profit_ladder_pullback_started", False) and daily_pnl_now <= ladder_floor:
                    should_exit_ladder = True
                    ladder_reason = f"Profit ladder lock exit @ Rs {daily_pnl_now:,.2f}"

            if should_exit_ladder:
                for sym, h in list(st.session_state.s_holdings.items()):
                    qty = int(h.get("qty", 0))
                    if qty <= 0:
                        continue
                    ltp = float(st.session_state.s_prices.get(
                        sym, h.get("avg", 0.0) or 0.0))
                    if ltp <= 0:
                        continue
                    _record_trade(sym, "SELL", qty, ltp, "Profit ladder exit")
                    actions.append(f"SELL {sym}: profit ladder @ Rs {ltp:.2f}")

                for sym, h in list(st.session_state.s_shorts.items()):
                    qty = int(h.get("qty", 0))
                    if qty <= 0:
                        continue
                    ltp = float(st.session_state.s_prices.get(
                        sym, h.get("avg", 0.0) or 0.0))
                    if ltp <= 0:
                        continue
                    _record_trade(sym, "COVER", qty, ltp, "Profit ladder exit")
                    actions.append(
                        f"COVER {sym}: profit ladder @ Rs {ltp:.2f}")

                st.session_state.s_profit_ladder_exited_day = today
                st.session_state.s_profit_ladder_armed = False
                st.session_state.s_profit_ladder_pullback_started = False
                actions.append(ladder_reason)
                return actions

    # Auto-sell long positions on SL/TP.
    for sym, h in list(st.session_state.s_holdings.items()):
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty <= 0 or avg <= 0 or ltp <= 0:
            continue

        stop_price = float(h.get("stop", avg * (1.0 - sl_pct)))
        target_price = float(h.get("target", avg * (1.0 + tp_pct)))
        if ltp <= stop_price:
            _record_trade(sym, "SELL", qty, ltp, "Auto SL")
            same_cycle_exited_symbols.add(sym)
            actions.append(f"SELL {sym}: SL hit @ Rs {ltp:.2f}")
        elif ltp >= target_price:
            _record_trade(sym, "SELL", qty, ltp, "Auto TP")
            same_cycle_exited_symbols.add(sym)
            actions.append(f"SELL {sym}: TP hit @ Rs {ltp:.2f}")

    # Auto-cover short positions on SL/TP.
    for sym, h in list(st.session_state.s_shorts.items()):
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        if qty <= 0 or avg <= 0 or ltp <= 0:
            continue

        stop_price = float(h.get("stop", avg * (1.0 + sl_pct)))
        target_price = float(h.get("target", avg * (1.0 - tp_pct)))
        if ltp >= stop_price:
            _record_trade(sym, "COVER", qty, ltp, "Auto short SL")
            same_cycle_exited_symbols.add(sym)
            actions.append(f"COVER {sym}: short SL hit @ Rs {ltp:.2f}")
        elif ltp <= target_price:
            _record_trade(sym, "COVER", qty, ltp, "Auto short TP")
            same_cycle_exited_symbols.add(sym)
            actions.append(f"COVER {sym}: short TP hit @ Rs {ltp:.2f}")

    # Optional: exit holdings when sell signal is READY with sufficient score.
    signal_exits_done = 0
    if enable_signal_sell and (not sell_exit_df.empty):
        for _, row in sell_exit_df.iterrows():
            if signal_exits_done >= int(max_signal_exits_per_cycle):
                break

            sym = str(row.get("symbol", ""))
            if sym not in st.session_state.s_holdings:
                continue
            if str(row.get("sell_signal", "WAIT")) != "READY":
                continue

            score = float(
                row.get("effective_sell_score", row.get(
                    "sell_score", 0.0)) or 0.0
            )
            if score < float(min_sell_score):
                continue

            h = st.session_state.s_holdings.get(sym, {"qty": 0, "avg": 0.0})
            qty = int(h.get("qty", 0))
            if qty <= 0:
                continue

            ltp = float(st.session_state.s_prices.get(
                sym, row.get("price", 0.0) or 0.0))
            if ltp <= 0:
                continue

            _record_trade(sym, "SELL", qty, ltp,
                          f"Auto SELL signal (score {score:.1f})")
            same_cycle_exited_symbols.add(sym)
            actions.append(
                f"SELL {sym}: signal READY @ Rs {ltp:.2f} (score {score:.1f})")
            signal_exits_done += 1

    if (
        enable_profit_guard
        and market_now().time() >= profit_guard_after
        and float(st.session_state.get("s_peak_open_pnl", 0.0)) > 0.0
        and st.session_state.get("s_profit_guard_triggered_day", "") != today
    ):
        peak = float(st.session_state.get("s_peak_open_pnl", 0.0))
        now_pnl = _current_open_pnl()
        drawdown_pct = ((peak - now_pnl) / peak) * 100.0
        if drawdown_pct >= float(profit_guard_drawdown_pct):
            for sym, h in list(st.session_state.s_holdings.items()):
                qty = int(h.get("qty", 0))
                if qty <= 0:
                    continue
                ltp = float(st.session_state.s_prices.get(
                    sym, h.get("avg", 0.0) or 0.0))
                if ltp <= 0:
                    continue
                _record_trade(sym, "SELL", qty, ltp, "Profit guard square-off")
                same_cycle_exited_symbols.add(sym)
                actions.append(f"SELL {sym}: profit guard @ Rs {ltp:.2f}")

            for sym, h in list(st.session_state.s_shorts.items()):
                qty = int(h.get("qty", 0))
                if qty <= 0:
                    continue
                ltp = float(st.session_state.s_prices.get(
                    sym, h.get("avg", 0.0) or 0.0))
                if ltp <= 0:
                    continue
                _record_trade(sym, "COVER", qty, ltp,
                              "Profit guard square-off")
                same_cycle_exited_symbols.add(sym)
                actions.append(f"COVER {sym}: profit guard @ Rs {ltp:.2f}")

            st.session_state.s_profit_guard_triggered_day = today
            actions.append(
                f"Profit guard triggered: open PnL drawdown {drawdown_pct:.1f}% from peak"
            )

    # Intraday square-off for all open positions.
    if market_now().time() >= _square_off_time():
        for sym, h in list(st.session_state.s_holdings.items()):
            qty = int(h.get("qty", 0))
            if qty <= 0:
                continue
            ltp = float(st.session_state.s_prices.get(
                sym, h.get("avg", 0.0) or 0.0))
            if ltp <= 0:
                continue
            _record_trade(sym, "SELL", qty, ltp, "Auto square-off")
            actions.append(f"SELL {sym}: square-off @ Rs {ltp:.2f}")

        for sym, h in list(st.session_state.s_shorts.items()):
            qty = int(h.get("qty", 0))
            if qty <= 0:
                continue
            ltp = float(st.session_state.s_prices.get(
                sym, h.get("avg", 0.0) or 0.0))
            if ltp <= 0:
                continue
            _record_trade(sym, "COVER", qty, ltp, "Auto square-off")
            actions.append(f"COVER {sym}: square-off @ Rs {ltp:.2f}")
        return actions

    if not _in_entry_window():
        return actions

    if block_new_entries_on_guard and st.session_state.get("s_profit_guard_triggered_day", "") == today:
        actions.append("New entries blocked after profit guard trigger")
        return actions

    entries_today = _today_entry_count()
    if entries_today >= int(max_trades_day):
        actions.append(
            f"AUTO-BUY paused: max {max_trades_day} trades reached today")
        return actions

    open_positions = len(st.session_state.s_holdings) + \
        len(st.session_state.s_shorts)
    if open_positions >= int(max_positions):
        actions.append(
            f"AUTO-BUY paused: max {max_positions} open positions reached")
        return actions

    risk_budget = float(st.session_state.s_start) * (float(risk_pct) / 100.0)
    max_qty_limit = max(1, int(max_qty_per_trade))
    max_invest_per_trade = float(st.session_state.s_start) * (
        float(max_total_deployment_pct) / 100.0) * (float(max_trade_invest_pct) / 100.0)
    sl_events = _latest_stop_loss_event_by_symbol()
    regime = str(market_regime or "unknown").strip().lower()
    allow_buy_regime = regime in {"bullish", "mixed", "unknown"}
    allow_short_regime = regime in {"bearish", "mixed", "unknown"}

    if enable_regime_entry_gate and not allow_buy_regime:
        actions.append(f"BUY entries blocked by regime gate ({regime})")
    if enable_regime_entry_gate and not allow_short_regime:
        actions.append(f"SHORT entries blocked by regime gate ({regime})")

    def _refresh_ranked_entries() -> tuple[pd.DataFrame, pd.DataFrame]:
        _buy_df, _sell_df, _sell_exit_df, _ = _rank_signals(
            symbols=symbols,
            min_price=min_price,
            max_price=max_price,
            risk_pct=risk_pct,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            max_symbol_allocation_pct=max_symbol_allocation_pct,
            max_total_deployment_pct=max_total_deployment_pct,
            max_qty_per_trade=max_qty_per_trade,
            max_open_positions=max_positions,
            min_order_value=min_order_value,
            max_trade_invest_pct=max_trade_invest_pct,
        )
        bias_map = _symbol_bias_map()
        _buy_df, _sell_df, _ = _apply_effective_scores(
            _buy_df,
            _sell_df,
            pd.DataFrame(),
            bias_map,
        )
        return _buy_df, _sell_df

    while (
        entries_today < int(max_trades_day)
        and open_positions < int(max_positions)
        and (not enable_regime_entry_gate or allow_buy_regime)
    ):
        live_buy_df, _ = _refresh_ranked_entries()
        if live_buy_df.empty:
            break

        traded = False
        idle_minutes = _minutes_since_last_entry(now_naive, today)
        fallback_active = float(idle_minutes) >= float(
            idle_buy_fallback_minutes)
        latest_exits = _latest_exit_info_by_symbol(today)
        for _, row in live_buy_df.iterrows():
            if entries_today >= int(max_trades_day) or open_positions >= int(max_positions):
                break

            sym = str(row["symbol"])
            if sym in same_cycle_entered_symbols:
                actions.append(
                    f"BUY {sym} SKIPPED: already entered this cycle")
                continue
            if sym in st.session_state.s_holdings or sym in st.session_state.s_shorts:
                continue
            if int(sl_cooldown_after_stop_minutes) > 0:
                last_sl = sl_events.get(sym)
                if last_sl is not None:
                    sl_ts, sl_reason = last_sl
                    minutes_since_sl = (
                        now_naive - sl_ts).total_seconds() / 60.0
                    if minutes_since_sl < float(sl_cooldown_after_stop_minutes):
                        actions.append(
                            f"BUY {sym} SKIPPED: SL cooldown {minutes_since_sl:.1f}m < {int(sl_cooldown_after_stop_minutes)}m ({sl_reason})"
                        )
                        continue
            if sym in same_cycle_exited_symbols:
                actions.append(
                    f"BUY {sym} SKIPPED: same-cycle re-entry blocked")
                continue
            last_exit = latest_exits.get(sym)
            if last_exit is not None:
                exit_ts, exit_price = last_exit
                if int(reentry_cooldown_minutes) > 0:
                    minutes_since_exit = (
                        now_naive - exit_ts).total_seconds() / 60.0
                    if minutes_since_exit < float(reentry_cooldown_minutes):
                        actions.append(
                            f"BUY {sym} SKIPPED: cooldown {minutes_since_exit:.1f}m < {int(reentry_cooldown_minutes)}m")
                        continue
                if float(reentry_min_move_pct) > 0 and exit_price > 0:
                    price_now = float(row.get("price", 0.0) or 0.0)
                    move_pct = abs(price_now - exit_price) / \
                        exit_price * 100.0 if price_now > 0 else 0.0
                    if move_pct < float(reentry_min_move_pct):
                        actions.append(
                            f"BUY {sym} SKIPPED: re-entry move {move_pct:.2f}% < {float(reentry_min_move_pct):.2f}%")
                        continue
            if str(row.get("buy_signal", "WAIT")) != "READY":
                continue

            price = float(row.get("price", 0.0) or 0.0)
            score = float(
                row.get("effective_buy_score", row.get(
                    "buy_score", 0.0)) or 0.0
            )
            if price <= 0:
                continue
            if score < float(min_buy_score) and not fallback_active:
                actions.append(
                    f"BUY {sym} SKIPPED: score {score:.1f} < min {float(min_buy_score):.1f}")
                continue

            risk_per_share = max(price * float(sl_pct), 0.01)
            qty_by_risk = int(risk_budget // risk_per_share)
            qty_by_cash = int(st.session_state.s_cash // max(price, 1e-6))
            qty_by_trade_cap = int(max_invest_per_trade // max(price, 1e-6))
            qty = max(0, min(qty_by_risk, qty_by_cash,
                      qty_by_trade_cap, max_qty_limit))

            if qty <= 0:
                actions.append(
                    f"BUY {sym} SKIPPED: insufficient cash/risk budget")
                continue

            est_value = qty * price
            est_ch = _intraday_charges("BUY", est_value)
            if st.session_state.s_cash < (est_value + est_ch):
                actions.append(f"BUY {sym} SKIPPED: insufficient cash")
                continue

            buy_reason = "Auto BUY signal"
            if score < float(min_buy_score) and fallback_active:
                buy_reason = (
                    f"Auto BUY fallback after {int(idle_buy_fallback_minutes)}m idle"
                )
                actions.append(
                    f"BUY {sym} fallback: idle {idle_minutes:.1f}m, score {score:.1f} < min {float(min_buy_score):.1f}"
                )

            _record_trade(sym, "BUY", qty, price, buy_reason,
                          sl_pct=sl_pct, tp_pct=tp_pct)
            actions.append(f"BUY {sym} {qty}qty @ Rs {price:.2f}")
            same_cycle_entered_symbols.add(sym)
            entries_today += 1
            open_positions += 1
            traded = True
            break

        if not traded:
            break

    # Optional intraday short entries from sell signals.
    if enable_short_selling and (not enable_regime_entry_gate or allow_short_regime):
        while entries_today < int(max_trades_day) and open_positions < int(max_positions):
            _, live_sell_df = _refresh_ranked_entries()
            if live_sell_df.empty:
                break

            traded = False
            latest_exits = _latest_exit_info_by_symbol(today)
            for _, row in live_sell_df.iterrows():
                if entries_today >= int(max_trades_day) or open_positions >= int(max_positions):
                    break

                sym = str(row.get("symbol", ""))
                if sym in same_cycle_entered_symbols:
                    actions.append(
                        f"SHORT {sym} SKIPPED: already entered this cycle")
                    continue
                if sym in st.session_state.s_holdings or sym in st.session_state.s_shorts:
                    continue
                if int(sl_cooldown_after_stop_minutes) > 0:
                    last_sl = sl_events.get(sym)
                    if last_sl is not None:
                        sl_ts, sl_reason = last_sl
                        minutes_since_sl = (
                            now_naive - sl_ts).total_seconds() / 60.0
                        if minutes_since_sl < float(sl_cooldown_after_stop_minutes):
                            actions.append(
                                f"SHORT {sym} SKIPPED: SL cooldown {minutes_since_sl:.1f}m < {int(sl_cooldown_after_stop_minutes)}m ({sl_reason})"
                            )
                            continue
                if sym in same_cycle_exited_symbols:
                    actions.append(
                        f"SHORT {sym} SKIPPED: same-cycle re-entry blocked")
                    continue
                last_exit = latest_exits.get(sym)
                if last_exit is not None:
                    exit_ts, exit_price = last_exit
                    if int(reentry_cooldown_minutes) > 0:
                        minutes_since_exit = (
                            now_naive - exit_ts).total_seconds() / 60.0
                        if minutes_since_exit < float(reentry_cooldown_minutes):
                            actions.append(
                                f"SHORT {sym} SKIPPED: cooldown {minutes_since_exit:.1f}m < {int(reentry_cooldown_minutes)}m")
                            continue
                    if float(reentry_min_move_pct) > 0 and exit_price > 0:
                        price_now = float(row.get("price", 0.0) or 0.0)
                        move_pct = abs(price_now - exit_price) / \
                            exit_price * 100.0 if price_now > 0 else 0.0
                        if move_pct < float(reentry_min_move_pct):
                            actions.append(
                                f"SHORT {sym} SKIPPED: re-entry move {move_pct:.2f}% < {float(reentry_min_move_pct):.2f}%")
                            continue
                if str(row.get("sell_signal", "WAIT")) != "READY":
                    continue

                price = float(row.get("price", 0.0) or 0.0)
                score = float(
                    row.get("effective_sell_score", row.get(
                        "sell_score", 0.0)) or 0.0
                )
                if price <= 0 or score < float(min_short_score):
                    continue

                risk_per_share = max(price * float(sl_pct), 0.01)
                qty_by_risk = int(risk_budget // risk_per_share)
                qty_by_margin = int(
                    max(0.0, st.session_state.s_cash) // max(price * 0.2, 0.01))
                qty_by_trade_cap = int(
                    max_invest_per_trade // max(price, 1e-6))
                qty = max(0, min(qty_by_risk, qty_by_margin,
                          qty_by_trade_cap, max_qty_limit))

                if qty <= 0:
                    actions.append(
                        f"SHORT {sym} SKIPPED: insufficient cash/risk budget")
                    continue

                est_value = qty * price
                est_ch = _intraday_charges("SELL", est_value)
                est_margin = est_value * 0.2
                if st.session_state.s_cash < (est_margin + est_ch):
                    actions.append(
                        f"SHORT {sym} SKIPPED: insufficient margin cash")
                    continue

                _record_trade(sym, "SHORT", qty, price,
                              "Auto SHORT signal", sl_pct=sl_pct, tp_pct=tp_pct)
                actions.append(f"SHORT {sym} {qty}qty @ Rs {price:.2f}")
                same_cycle_entered_symbols.add(sym)
                entries_today += 1
                open_positions += 1
                traded = True
                break

            if not traded:
                break

    _refresh_holding_prices()
    return actions


def _render_app_logs() -> None:
    """Display application logs in a scrollable container."""
    logs = st.session_state.get("s_app_logs", [])
    if not logs:
        return
    
    with st.expander("📋 Activity Logs", expanded=False):
        # Create a scrollable container using a container with fixed height via CSS
        log_container = st.container()
        
        # Display logs in reverse order (latest first)
        with log_container:
            for log_entry in reversed(logs[-50:]):  # Show last 50 logs
                level = log_entry.get("level", "INFO")
                message = log_entry.get("message", "")
                timestamp = log_entry.get("timestamp", "")
                
                # Color code based on level
                if level == "ERROR":
                    st.markdown(f"🔴 **{timestamp}** ERROR: {message}")
                elif level == "WARNING":
                    st.markdown(f"🟡 **{timestamp}** WARNING: {message}")
                elif level == "INFO":
                    st.markdown(f"ℹ️ **{timestamp}** {message}")
                else:
                    st.markdown(f"⚪ **{timestamp}** {message}")
        
        # Add a button to clear logs
        if st.button("Clear Logs", key="clear_logs_btn"):
            st.session_state.s_app_logs = []
            st.rerun()


def _portfolio_view() -> tuple[pd.DataFrame, float, float]:
    rows: list[dict[str, Any]] = []
    unreal = 0.0
    total_invested = 0.0
    for sym, h in st.session_state.s_holdings.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        stop_price = float(h.get("stop", avg * 0.992))
        target_price = float(h.get("target", avg * 1.016))
        invested = avg * qty
        pnl = (ltp - avg) * qty
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0
        unreal += pnl
        total_invested += invested
        rows.append(
            {
                "symbol": sym,
                "side": "LONG",
                "qty": qty,
                "avg": round(avg, 2),
                "ltp": round(ltp, 2),
                "stop": round(stop_price, 2),
                "target": round(target_price, 2),
                "invested": round(invested, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            }
        )

    for sym, h in st.session_state.s_shorts.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(st.session_state.s_prices.get(sym, avg))
        stop_price = float(h.get("stop", avg * 1.008))
        target_price = float(h.get("target", avg * 0.984))
        invested = avg * qty
        pnl = (avg - ltp) * qty
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0
        unreal += pnl
        total_invested += invested
        rows.append(
            {
                "symbol": sym,
                "side": "SHORT",
                "qty": qty,
                "avg": round(avg, 2),
                "ltp": round(ltp, 2),
                "stop": round(stop_price, 2),
                "target": round(target_price, 2),
                "invested": round(invested, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            }
        )
    return pd.DataFrame(rows), float(unreal), float(total_invested)


st.set_page_config(page_title="Simple Budget Trading Simulator",
                   page_icon="\U0001f4b0", layout="wide")
st.title("\U0001f4b0 Simple Budget-Based Trading Simulator")

# Initialize session state keys for all sidebar inputs
_sidebar_keys = [
    "selected_market",
    "total_capital",
    "risk_pct",
    "max_trades_day",
    "max_open_positions",
    "max_symbol_allocation_pct",
    "max_total_deployment_pct",
    "max_qty_per_trade",
    "max_trade_invest_pct",
    "scan_symbol_count",
    "min_price",
    "max_price",
    "min_buy_score",
    "auto_budget_filters",
    "min_order_value",
    "idle_buy_fallback_minutes",
    "topup_target_qty",
    "topup_ignore_cash_check",
    "sl_pct",
    "tp_pct",
    "enable_signal_sell",
    "min_sell_score",
    "max_signal_exits_per_cycle",
    "reentry_cooldown_minutes",
    "reentry_min_move_pct",
    "enable_short_selling",
    "min_short_score",
    "enable_regime_entry_gate",
    "sl_cooldown_after_stop_minutes",
    "enable_learning_agent",
    "auto_apply_learning_suggestions",
    "enable_profit_guard",
    "profit_guard_drawdown_pct",
    "profit_guard_after_hhmm",
    "block_new_entries_on_guard",
    "daily_profit_target",
    "full_auto_paper_mode",
    "auto_trade_on",
    "auto_refresh_on",
    "refresh_seconds",
    "optimizer_auto_run",
    "optimizer_interval_minutes",
    "optimizer_lookback_trades",
    "optimizer_min_train_trades",
    "optimizer_quality_threshold",
]

# Initialize all sidebar keys in session state if not present
for key in _sidebar_keys:
    if key not in st.session_state:
        st.session_state[key] = None

if "selected_market" not in st.session_state or st.session_state.selected_market is None:
    st.session_state.selected_market = "NSE"
    _app_log("info", "Initialized market selector to NSE")

with st.sidebar:
    st.header("Market")
    selected = st.radio(
        "Trading Market",
        options=["NSE", "US"],
        format_func=lambda key: str(MARKET_CONFIG[key]["label"]),
        key="selected_market",
        help="Each market keeps its own paper-trade state and ML model.",
    )
    if selected != st.session_state.get("_last_market"):
        st.session_state._last_market = selected
        _app_log("info", f"Switched to {MARKET_CONFIG[selected]['label']} market")

saved_state = _read_saved_state()
saved_ui = saved_state.get("ui_config", {}) if isinstance(
    saved_state, dict) else {}
watchlist = _watchlist()
currency_symbol = _currency_symbol()

with st.sidebar:
    st.header("Setup")
    total_capital = float(st.number_input(
        f"Total Capital ({currency_symbol})", min_value=1000.0, value=float(saved_ui.get("total_capital", saved_state.get("start", 200000.0))), step=1000.0))
    risk_pct = float(st.slider("Risk Per Trade (%)",
                     min_value=0.1, max_value=5.0, value=float(saved_ui.get("risk_pct", 0.5)), step=0.1))
    max_trades_day = int(st.number_input(
        "Max Trades Per Day", min_value=1, max_value=200, value=int(saved_ui.get("max_trades_day", 3)), step=1))
    max_open_positions = int(st.number_input(
        "Max Open Positions", min_value=1, max_value=10, value=int(saved_ui.get("max_open_positions", 3)), step=1))
    max_symbol_allocation_pct = float(st.slider(
        "Max Allocation Per Symbol (%)", min_value=5.0, max_value=60.0,
        value=float(saved_ui.get("max_symbol_allocation_pct", 15.0)), step=1.0))
    max_total_deployment_pct = float(st.slider(
        "Max Total Deployment (%)", min_value=20.0, max_value=100.0,
        value=float(saved_ui.get("max_total_deployment_pct", 80.0)), step=1.0))
    max_qty_per_trade = int(st.number_input(
        "Max Qty Per Trade", min_value=1, max_value=100000,
        value=int(saved_ui.get("max_qty_per_trade", 300)), step=1))
    max_trade_invest_pct = float(st.slider(
        "Max Invest Per Trade (% of deployed cap)", min_value=2.0, max_value=50.0,
        value=float(saved_ui.get("max_trade_invest_pct", 10.0)), step=1.0,
        help="Caps single-trade investment to this % of total deployed capital. E.g. 10% of Rs 16L cap = Rs 1.6L max per trade."))
    scan_symbol_count = int(st.number_input(
        "Symbols To Scan",
        min_value=5,
        max_value=len(watchlist),
        value=int(saved_ui.get("scan_symbol_count", min(25, len(watchlist)))),
        step=1,
        help="Limits active scan universe for faster refresh. Uses the first N symbols from watchlist.",
    ))

    st.header("Signal Filter")
    min_price = float(st.number_input(f"Min Price ({currency_symbol})",
                      min_value=0.0, max_value=50000.0, value=float(saved_ui.get("min_price", 50.0)), step=10.0))
    max_price = float(st.number_input(
        f"Max Price ({currency_symbol})", min_value=1.0, max_value=50000.0, value=float(saved_ui.get("max_price", 1800.0)), step=10.0))
    min_buy_score = float(st.slider(
        "Minimum Buy Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_buy_score", 40.0)), step=1.0))
    auto_budget_filters = st.checkbox(
        "Auto-set order/profit filters from budget + open slots",
        value=bool(saved_ui.get("auto_budget_filters", True)),
        help="Recomputes minimum order value each refresh using cash, deployment headroom, and remaining open-position slots.",
    )
    saved_min_order_value = float(saved_ui.get("min_order_value", 50000.0))
    saved_min_order_value = min(5000000.0, max(100.0, saved_min_order_value))
    min_order_value = float(st.number_input(
        f"Minimum Order Value ({currency_symbol})", min_value=100.0, max_value=5000000.0,
        value=float(saved_min_order_value), step=1000.0,
        help="Avoids tiny orders that get eaten by charges"))
    idle_buy_fallback_minutes = int(st.number_input(
        "Idle Buy Fallback After (Minutes)", min_value=1, max_value=240,
        value=int(saved_ui.get("idle_buy_fallback_minutes", 15)), step=1,
        help="If no BUY/SHORT entry happens for this long, allow the best READY buy even below min score"
    ))
    topup_target_qty = int(st.number_input(
        "Top-up Existing Tiny Positions To Qty", min_value=1, max_value=10000, value=int(saved_ui.get("topup_target_qty", 200)), step=1,
        help="Only used when you click top-up; does not affect normal auto-entry sizing"))
    topup_ignore_cash_check = st.checkbox(
        "Testing: ignore cash check for top-up",
        value=bool(saved_ui.get("topup_ignore_cash_check", True)),
        help="For testing only. Can make cash negative.",
    )

    st.header("Risk / Exit")
    sl_pct = float(st.slider("Stop Loss (%)", min_value=0.2,
                   max_value=3.0, value=float(saved_ui.get("sl_pct_display", 0.8)), step=0.1)) / 100.0
    tp_pct = float(st.slider("Take Profit (%)", min_value=0.2,
                   max_value=6.0, value=float(saved_ui.get("tp_pct_display", 1.6)), step=0.1)) / 100.0
    enable_signal_sell = st.checkbox(
        "Enable signal-based auto-sell", value=bool(saved_ui.get("enable_signal_sell", True)))
    min_sell_score = float(st.slider(
        "Minimum Sell Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_sell_score", 20.0)), step=1.0))
    max_signal_exits_per_cycle = int(st.number_input(
        "Max Signal Exits Per Refresh", min_value=1, max_value=5, value=int(saved_ui.get("max_signal_exits_per_cycle", 2)), step=1))
    reentry_cooldown_minutes = int(st.number_input(
        "Re-entry cooldown (minutes)", min_value=0, max_value=120, value=int(saved_ui.get("reentry_cooldown_minutes", 10)), step=1))
    reentry_min_move_pct = float(st.slider(
        "Min price move for re-entry (%)", min_value=0.0, max_value=3.0, value=float(saved_ui.get("reentry_min_move_pct", 0.20)), step=0.05))
    enable_short_selling = st.checkbox(
        "Enable intraday short selling", value=bool(saved_ui.get("enable_short_selling", True)))
    min_short_score = float(st.slider(
        "Minimum Short Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_short_score", 20.0)), step=1.0))
    enable_regime_entry_gate = st.checkbox(
        "Gate entries by market regime",
        value=bool(saved_ui.get("enable_regime_entry_gate", True)),
        help="Blocks longs in bearish/sideways and blocks shorts in bullish/sideways regimes.",
    )
    sl_cooldown_after_stop_minutes = int(st.number_input(
        "Stop-loss cooldown per symbol (minutes)",
        min_value=0,
        max_value=1440,
        value=int(saved_ui.get("sl_cooldown_after_stop_minutes", 180)),
        step=5,
        help="After a symbol hits stop-loss, block fresh entries in that symbol for this duration.",
    ))

    st.header("Learning Agent")
    enable_learning_agent = st.checkbox(
        "Enable learning + market research", value=bool(saved_ui.get("enable_learning_agent", True))
    )
    auto_apply_learning_suggestions = st.checkbox(
        "Auto-apply learning suggestions to settings",
        value=bool(saved_ui.get("auto_apply_learning_suggestions", True)),
        help="Automatically updates thresholds/risk filters from Learning Agent suggestions and persists them.",
    )
    enable_profit_guard = st.checkbox(
        "Enable profit protection rule", value=bool(saved_ui.get("enable_profit_guard", True))
    )
    profit_guard_drawdown_pct = float(st.slider(
        "Profit give-back trigger (%)",
        min_value=10.0,
        max_value=80.0,
        value=float(saved_ui.get("profit_guard_drawdown_pct", 35.0)),
        step=1.0,
    ))
    profit_guard_after_hhmm = int(st.number_input(
        "Profit guard active after (HHMM)",
        min_value=900,
        max_value=1515,
        value=int(saved_ui.get("profit_guard_after_hhmm", 1500)),
        step=5,
    ))
    block_new_entries_on_guard = st.checkbox(
        "Block new entries after guard trigger",
        value=bool(saved_ui.get("block_new_entries_on_guard", True)),
    )

    st.header("Target")
    daily_profit_target = float(st.number_input(
        f"Daily Profit Target ({currency_symbol})", min_value=500.0, value=float(saved_ui.get("daily_profit_target", 4000.0)), step=100.0))

    st.header("Run")
    full_auto_paper_mode = st.checkbox(
        "Full Auto Paper Mode (hands-free)",
        value=bool(saved_ui.get("full_auto_paper_mode", False)),
        help="Keeps paper trading only, but auto-enables trading, refresh, learning, ML scoring, and optimizer.",
    )
    auto_trade_on = st.checkbox("Enable Auto Paper Trading", value=bool(
        saved_ui.get("auto_trade_on", True)))
    auto_refresh_on = st.checkbox("Auto Refresh", value=bool(
        saved_ui.get("auto_refresh_on", True)))
    refresh_seconds = int(st.number_input(
        "Refresh Seconds", min_value=5, max_value=120, value=int(saved_ui.get("refresh_seconds", 10)), step=5,
        help="Updates prices and P&L on the portfolio metrics bar. Faster = more responsive but higher CPU."
    ))
    st.subheader("Optimizer")
    optimizer_auto_run = st.checkbox(
        "Auto-run optimizer",
        value=bool(saved_ui.get("optimizer_auto_run", False)),
    )
    optimizer_interval_minutes = int(st.number_input(
        "Optimizer interval (minutes)",
        min_value=1,
        max_value=720,
        value=int(saved_ui.get("optimizer_interval_minutes", 30)),
        step=1,
    ))
    optimizer_lookback_trades = int(st.number_input(
        "Optimizer lookback trades",
        min_value=20,
        max_value=2000,
        value=int(saved_ui.get("optimizer_lookback_trades", 200)),
        step=10,
    ))
    optimizer_min_train_trades = int(st.number_input(
        "Optimizer min train trades",
        min_value=20,
        max_value=1000,
        value=int(saved_ui.get("optimizer_min_train_trades", 50)),
        step=5,
    ))
    optimizer_quality_threshold = float(st.slider(
        "Optimizer quality threshold",
        min_value=40.0,
        max_value=80.0,
        value=float(saved_ui.get("optimizer_quality_threshold", 55.0)),
        step=1.0,
    ))
    run_optimizer_now_btn = st.button(
        "Run Optimizer Now", use_container_width=True)
    topup_small_positions_btn = st.button(
        "Top Up Existing Tiny Positions", use_container_width=True)
    reset_btn = st.button("Reset Simulator", use_container_width=True)

if full_auto_paper_mode:
    auto_trade_on = True
    auto_refresh_on = True
    refresh_seconds = min(int(refresh_seconds), 10)
    enable_learning_agent = True
    auto_apply_learning_suggestions = True
    enable_signal_sell = True
    enable_profit_guard = True
    enable_regime_entry_gate = True
    optimizer_auto_run = True

    ui_cfg = st.session_state.get("s_ui_config", {})
    if not isinstance(ui_cfg, dict):
        ui_cfg = {}
    if not bool(ui_cfg.get("enable_ml_scoring", False)):
        ui_cfg["enable_ml_scoring"] = True
        st.session_state.s_ui_config = ui_cfg
        _save_state()

_init_state(total_capital)

# If user changed Total Capital in the sidebar, scale cash and start accordingly
_saved_start = float(st.session_state.get("s_start", total_capital))
if abs(total_capital - _saved_start) > 0.01:
    _capital_delta = total_capital - _saved_start
    st.session_state.s_cash = float(st.session_state.s_cash) + _capital_delta
    st.session_state.s_start = float(total_capital)
    _save_state()

current_ui_config = {
    "total_capital": float(total_capital),
    "risk_pct": float(risk_pct),
    "max_trades_day": int(max_trades_day),
    "max_open_positions": int(max_open_positions),
    "max_symbol_allocation_pct": float(max_symbol_allocation_pct),
    "max_total_deployment_pct": float(max_total_deployment_pct),
    "max_qty_per_trade": int(max_qty_per_trade),
    "max_trade_invest_pct": float(max_trade_invest_pct),
    "scan_symbol_count": int(scan_symbol_count),
    "min_price": float(min_price),
    "max_price": float(max_price),
    "min_buy_score": float(min_buy_score),
    "auto_budget_filters": bool(auto_budget_filters),
    "min_order_value": float(min_order_value),
    "idle_buy_fallback_minutes": int(idle_buy_fallback_minutes),
    "topup_target_qty": int(topup_target_qty),
    "topup_ignore_cash_check": bool(topup_ignore_cash_check),
    "sl_pct_display": float(sl_pct * 100.0),
    "tp_pct_display": float(tp_pct * 100.0),
    "enable_signal_sell": bool(enable_signal_sell),
    "min_sell_score": float(min_sell_score),
    "max_signal_exits_per_cycle": int(max_signal_exits_per_cycle),
    "reentry_cooldown_minutes": int(reentry_cooldown_minutes),
    "reentry_min_move_pct": float(reentry_min_move_pct),
    "enable_short_selling": bool(enable_short_selling),
    "min_short_score": float(min_short_score),
    "enable_regime_entry_gate": bool(enable_regime_entry_gate),
    "sl_cooldown_after_stop_minutes": int(sl_cooldown_after_stop_minutes),
    "enable_learning_agent": bool(enable_learning_agent),
    "auto_apply_learning_suggestions": bool(auto_apply_learning_suggestions),
    "enable_profit_guard": bool(enable_profit_guard),
    "profit_guard_drawdown_pct": float(profit_guard_drawdown_pct),
    "profit_guard_after_hhmm": int(profit_guard_after_hhmm),
    "block_new_entries_on_guard": bool(block_new_entries_on_guard),
    "daily_profit_target": float(daily_profit_target),
    "full_auto_paper_mode": bool(full_auto_paper_mode),
    "auto_trade_on": bool(auto_trade_on),
    "auto_refresh_on": bool(auto_refresh_on),
    "refresh_seconds": int(refresh_seconds),
    "enable_ml_scoring": bool(st.session_state.get("s_ui_config", {}).get(
        "enable_ml_scoring", saved_ui.get("enable_ml_scoring", False))),
    "optimizer_auto_run": bool(optimizer_auto_run),
    "optimizer_interval_minutes": int(optimizer_interval_minutes),
    "optimizer_lookback_trades": int(optimizer_lookback_trades),
    "optimizer_min_train_trades": int(optimizer_min_train_trades),
    "optimizer_quality_threshold": float(optimizer_quality_threshold),
}

if st.session_state.get("s_ui_config", {}) != current_ui_config:
    st.session_state.s_ui_config = current_ui_config
    _save_state()

config_guard_messages: list[str] = []
if auto_budget_filters:
    open_positions_now = len(st.session_state.get(
        "s_holdings", {})) + len(st.session_state.get("s_shorts", {}))
    exposure_now = _current_gross_exposure()
    auto_order_floor, slot_budget_now, remaining_slots_now = _auto_filters_from_budget_slots(
        total_capital=total_capital,
        cash_now=float(st.session_state.get("s_cash", total_capital)),
        current_exposure=exposure_now,
        current_open_positions=open_positions_now,
        max_open_positions=max_open_positions,
        max_symbol_allocation_pct=max_symbol_allocation_pct,
        max_total_deployment_pct=max_total_deployment_pct,
        max_qty_per_trade=max_qty_per_trade,
        max_price=max_price,
    )
    min_order_value = float(auto_order_floor)
    current_ui_config["min_order_value"] = float(min_order_value)
    config_guard_messages.append(
        "Auto-set filters from budget/slots: "
        f"slot budget Rs {slot_budget_now:,.0f}, remaining slots {remaining_slots_now}, "
        f"min order Rs {min_order_value:,.0f}"
    )

feasible_order_cap = _max_feasible_order_value(
    total_capital=total_capital,
    cash_now=float(st.session_state.get("s_cash", total_capital)),
    max_symbol_allocation_pct=max_symbol_allocation_pct,
    max_total_deployment_pct=max_total_deployment_pct,
    max_qty_per_trade=max_qty_per_trade,
    max_price=max_price,
)
if float(min_order_value) > feasible_order_cap + 1e-9:
    min_order_value = float(feasible_order_cap)
    current_ui_config["min_order_value"] = float(min_order_value)
    config_guard_messages.append(
        f"Capped min order value to feasible max: Rs {float(min_order_value):,.0f}"
    )

if config_guard_messages:
    st.session_state.s_ui_config = current_ui_config
    _save_state()

if reset_btn:
    state_file = _state_file()
    if state_file.exists():
        state_file.unlink(missing_ok=True)
    st.session_state.clear()
    st.rerun()

# Shuffle the watchlist daily so the scan pool rotates across days.
# Same seed within a day means the order is stable for the whole session.
_day_seed = int(market_now().strftime("%Y%m%d"))
_shuffled_wl = list(watchlist)
_rng_mod.Random(_day_seed).shuffle(_shuffled_wl)
# Always ensure any currently open positions are included in the scan
# so their exit signals are never missed.
_open_syms = list(st.session_state.get("s_holdings", {}).keys()) + \
    list(st.session_state.get("s_shorts", {}).keys())
for _s in _open_syms:
    if _s in _shuffled_wl:
        _shuffled_wl.remove(_s)
        _shuffled_wl.insert(0, _s)
_n = max(5, min(int(scan_symbol_count), len(_shuffled_wl)))
active_watchlist = _shuffled_wl[:_n]

buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
    active_watchlist,
    min_price=min_price,
    max_price=max_price,
    risk_pct=risk_pct,
    sl_pct=sl_pct,
    tp_pct=tp_pct,
    max_symbol_allocation_pct=max_symbol_allocation_pct,
    max_total_deployment_pct=max_total_deployment_pct,
    max_qty_per_trade=max_qty_per_trade,
    max_open_positions=max_open_positions,
    min_order_value=min_order_value,
    max_trade_invest_pct=max_trade_invest_pct,
)

learning_memory = _update_learning_memory()
market_research = _market_research_from_signals(buy_df, sell_exit_df)
symbol_bias = _symbol_bias_map()

agent_plan = _agent_tuning_plan(
    base_min_buy_score=min_buy_score,
    base_min_short_score=min_short_score,
    base_tp_pct=tp_pct,
    base_idle_buy_fallback_minutes=idle_buy_fallback_minutes,
    base_reentry_cooldown_minutes=reentry_cooldown_minutes,
    learning_memory=learning_memory,
    market_research=market_research,
)

effective_min_buy_score = min_buy_score
effective_min_short_score = min_short_score
effective_tp_pct = tp_pct
learning_apply_messages: list[str] = []
ranking_filters_changed = False
if enable_learning_agent:
    effective_min_buy_score = float(agent_plan["effective_min_buy_score"])
    effective_min_short_score = float(agent_plan["effective_min_short_score"])
    effective_tp_pct = float(agent_plan["effective_tp_pct"])

    if auto_apply_learning_suggestions:
        old_tp_pct = float(tp_pct)
        old_idle_fallback = int(idle_buy_fallback_minutes)
        old_reentry_cooldown = int(reentry_cooldown_minutes)

        min_buy_score = float(effective_min_buy_score)
        min_short_score = float(effective_min_short_score)
        tp_pct = float(effective_tp_pct)
        idle_buy_fallback_minutes = int(
            agent_plan.get("suggested_idle_buy_fallback_minutes",
                           idle_buy_fallback_minutes)
        )
        reentry_cooldown_minutes = int(
            agent_plan.get("suggested_reentry_cooldown_minutes",
                           reentry_cooldown_minutes)
        )

        effective_min_buy_score = float(min_buy_score)
        effective_min_short_score = float(min_short_score)
        effective_tp_pct = float(tp_pct)

        ranking_filters_changed = (
            abs(float(tp_pct) - old_tp_pct) > 1e-9
        )

        if abs(float(min_buy_score) - float(current_ui_config.get("min_buy_score", min_buy_score))) > 1e-9:
            learning_apply_messages.append(
                f"Applied min buy score: {float(min_buy_score):.1f}"
            )
        if abs(float(min_short_score) - float(current_ui_config.get("min_short_score", min_short_score))) > 1e-9:
            learning_apply_messages.append(
                f"Applied min short score: {float(min_short_score):.1f}"
            )
        if abs(float(tp_pct * 100.0) - float(current_ui_config.get("tp_pct_display", tp_pct * 100.0))) > 1e-9:
            learning_apply_messages.append(
                f"Applied TP: {float(tp_pct) * 100.0:.2f}%"
            )
        if int(idle_buy_fallback_minutes) != int(current_ui_config.get("idle_buy_fallback_minutes", idle_buy_fallback_minutes)):
            learning_apply_messages.append(
                f"Applied idle fallback: {int(idle_buy_fallback_minutes)}m"
            )
        if int(reentry_cooldown_minutes) != int(current_ui_config.get("reentry_cooldown_minutes", reentry_cooldown_minutes)):
            learning_apply_messages.append(
                f"Applied re-entry cooldown: {int(reentry_cooldown_minutes)}m"
            )
        current_ui_config["min_buy_score"] = float(min_buy_score)
        current_ui_config["min_short_score"] = float(min_short_score)
        current_ui_config["tp_pct_display"] = float(tp_pct * 100.0)
        current_ui_config["idle_buy_fallback_minutes"] = int(
            idle_buy_fallback_minutes)
        current_ui_config["reentry_cooldown_minutes"] = int(
            reentry_cooldown_minutes)
        st.session_state.s_ui_config = current_ui_config
        _save_state()

# Keep ranking profit filters aligned with the final TP used by strategy.
if abs(float(effective_tp_pct) - float(tp_pct)) > 1e-9 or ranking_filters_changed:
    buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
        active_watchlist,
        min_price=min_price,
        max_price=max_price,
        risk_pct=risk_pct,
        sl_pct=sl_pct,
        tp_pct=effective_tp_pct,
        max_symbol_allocation_pct=max_symbol_allocation_pct,
        max_total_deployment_pct=max_total_deployment_pct,
        max_qty_per_trade=max_qty_per_trade,
        max_open_positions=max_open_positions,
        min_order_value=min_order_value,
        max_trade_invest_pct=max_trade_invest_pct,
    )

buy_df, sell_df, sell_exit_df = _apply_effective_scores(
    buy_df,
    sell_df,
    sell_exit_df,
    symbol_bias,
)

actions: list[str] = []
if auto_trade_on:
    _app_log("info", "Starting auto-trade cycle...")
    actions = _auto_paper_cycle(
        buy_df=buy_df,
        sell_df=sell_df,
        sell_exit_df=sell_exit_df,
        risk_pct=risk_pct,
        max_trades_day=max_trades_day,
        max_positions=max_open_positions,
        sl_pct=sl_pct,
        tp_pct=effective_tp_pct,
        min_buy_score=effective_min_buy_score,
        enable_signal_sell=enable_signal_sell,
        min_sell_score=min_sell_score,
        max_signal_exits_per_cycle=max_signal_exits_per_cycle,
        enable_short_selling=enable_short_selling,
        min_short_score=effective_min_short_score,
        enable_profit_guard=enable_profit_guard,
        profit_guard_drawdown_pct=profit_guard_drawdown_pct,
        profit_guard_after=_hhmm_to_time(profit_guard_after_hhmm),
        block_new_entries_on_guard=block_new_entries_on_guard,
        daily_profit_target=daily_profit_target,
        reentry_cooldown_minutes=reentry_cooldown_minutes,
        reentry_min_move_pct=reentry_min_move_pct,
        enable_regime_entry_gate=enable_regime_entry_gate,
        market_regime=str(market_research.get("regime", "unknown")),
        sl_cooldown_after_stop_minutes=sl_cooldown_after_stop_minutes,
        max_qty_per_trade=max_qty_per_trade,
        symbols=active_watchlist,
        min_price=min_price,
        max_price=max_price,
        max_symbol_allocation_pct=max_symbol_allocation_pct,
        max_total_deployment_pct=max_total_deployment_pct,
        min_order_value=min_order_value,
        idle_buy_fallback_minutes=idle_buy_fallback_minutes,
        max_trade_invest_pct=max_trade_invest_pct,
    )
    
    if actions:
        _app_log("info", f"Auto-trade cycle complete: {len(actions)} action(s)")
        # Show latest Top-5 after any executed trade in this cycle.
        buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
            active_watchlist,
            min_price=min_price,
            max_price=max_price,
            risk_pct=risk_pct,
            sl_pct=sl_pct,
            tp_pct=effective_tp_pct,
            max_symbol_allocation_pct=max_symbol_allocation_pct,
            max_total_deployment_pct=max_total_deployment_pct,
            max_qty_per_trade=max_qty_per_trade,
            max_open_positions=max_open_positions,
            min_order_value=min_order_value,
            max_trade_invest_pct=max_trade_invest_pct,
        )

        buy_df, sell_df, sell_exit_df = _apply_effective_scores(
            buy_df,
            sell_df,
            sell_exit_df,
            symbol_bias,
        )

if topup_small_positions_btn:
    topup_actions = _top_up_small_holdings(
        target_qty=topup_target_qty,
        sl_pct=sl_pct,
        tp_pct=effective_tp_pct,
        ignore_cash_check=topup_ignore_cash_check,
    )
    if topup_actions:
        actions.extend(topup_actions)
    else:
        actions.append(
            f"No open long positions below qty {int(topup_target_qty)} to top up")

clean_closed_trade_count, clean_export_err = _auto_export_clean_closed_trades()

optimizer_summary = st.session_state.get("s_optimizer_summary")
optimizer_artifacts = st.session_state.get("s_optimizer_artifacts")
optimizer_last_run_ts = float(
    st.session_state.get("s_optimizer_last_run_ts", 0.0))
_now_ts = ist_now().timestamp()
_should_auto_run_optimizer = bool(optimizer_auto_run) and (
    optimizer_last_run_ts <= 0.0
    or (_now_ts - optimizer_last_run_ts) >= (float(optimizer_interval_minutes) * 60.0)
)
_should_run_optimizer = bool(
    run_optimizer_now_btn) or _should_auto_run_optimizer
if _should_run_optimizer:
    with st.spinner("Running optimizer analysis..."):
        _summary, _artifacts, _optimizer_err = _run_optimizer_from_dashboard(
            lookback_trades=optimizer_lookback_trades,
            min_train_trades=optimizer_min_train_trades,
            quality_threshold=optimizer_quality_threshold,
        )
    if _optimizer_err is None:
        st.session_state.s_optimizer_summary = _summary
        st.session_state.s_optimizer_artifacts = _artifacts
        st.session_state.s_optimizer_last_run_ts = _now_ts
        optimizer_summary = _summary
        optimizer_artifacts = _artifacts
    else:
        st.session_state.s_optimizer_error = _optimizer_err

# Display portfolio metrics with auto price refresh every page load/refresh
_quick_portfolio_metrics()

# Display application activity logs
_render_app_logs()

# Get holdings dataframe for the Open Positions section below
holdings_df, _, _ = _portfolio_view()

# Yesterday's PnL from history CSV (for sidebar display if needed)
_yesterday = (market_now() - timedelta(days=1)).strftime("%Y-%m-%d")
_daily_hist_file = Path("outputs") / "daily_pnl_history.csv"
yesterday_net = None
if _daily_hist_file.exists():
    try:
        _hist = pd.read_csv(_daily_hist_file)
        _row = _hist[_hist["trade_date"] == _yesterday]
        if not _row.empty:
            yesterday_net = float(_row.iloc[-1]["net_pnl"])
    except Exception:
        pass

with st.expander("\U0001f916 Learning Agent: Tomorrow Plan", expanded=True):
    latest_learning = agent_plan.get(
        "latest_learning") if isinstance(agent_plan, dict) else None
    if isinstance(latest_learning, dict):
        st.write(
            f"Latest learned day: {latest_learning.get('date', '-')} | "
            f"Closed trades: {int(latest_learning.get('closed_trades', 0))} | "
            f"Win rate: {float(latest_learning.get('win_rate', 0.0)):.1f}% | "
            f"Net: Rs {float(latest_learning.get('net', 0.0)):,.2f}"
        )
    else:
        st.write(
            "Not enough closed-trade history yet. Agent will learn as trade history grows.")

    st.write(
        f"Market regime: {market_research.get('regime', 'unknown')} | "
        f"Avg pchange: {float(market_research.get('avg_pchange', 0.0)):.2f}% | "
        f"Volatility proxy: {float(market_research.get('volatility', 0.0)):.2f}"
    )

    st.write(
        f"Effective thresholds now -> Buy score: {effective_min_buy_score:.1f}, "
        f"Short score: {effective_min_short_score:.1f}, TP: {effective_tp_pct * 100.0:.2f}%"
    )

    notes = agent_plan.get("notes", []) if isinstance(agent_plan, dict) else []
    if notes:
        for note in notes:
            st.caption(f"- {note}")
    if learning_apply_messages:
        for msg in learning_apply_messages:
            st.caption(f"- {msg}")
    if config_guard_messages:
        for msg in config_guard_messages:
            st.caption(f"- {msg}")

    symbol_rows = learning_memory.get(
        "symbols", []) if isinstance(learning_memory, dict) else []
    if symbol_rows:
        sorted_rows = sorted(
            symbol_rows,
            key=lambda r: float(r.get("bias", 0.0) or 0.0),
            reverse=True,
        )
        top_syms = [str(r.get("symbol", ""))
                    for r in sorted_rows[:3] if str(r.get("symbol", ""))]
        avoid_syms = [
            str(r.get("symbol", ""))
            for r in sorted_rows[-3:]
            if str(r.get("symbol", "")) and float(r.get("bias", 0.0) or 0.0) < 0
        ]
        if top_syms:
            st.write(f"Preferred symbols: {', '.join(top_syms)}")
        if avoid_syms:
            st.write(f"Avoid/low-priority symbols: {', '.join(avoid_syms)}")

with st.expander("🤖 ML Market Learning", expanded=False):
    st.write("Train ensemble ML model on **2 months historical market data** + personal trade history to improve symbol scoring.")
    st.caption("First run fetches 60 days of OHLCV data; subsequent runs use cached data for speed. Daily incremental retraining recommended.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Train ML Model", key="btn_train_ml", help="Fetch 2 months historical data + train on personal trades"):
            if train_market_learning_model is None:
                st.error(
                    "ML module not available; install: pip install scikit-learn yfinance")
            else:
                with st.spinner("🔄 Fetching 60 days historical data + training model..."):
                    try:
                        result = train_market_learning_model(
                            _state_file(), active_watchlist, use_historical_data=True, historical_days=60)
                        status = str(result.get("status", "unknown"))
                        reason = str(result.get("reason", "")).strip()
                        if status == "trained":
                            st.success("✅ trained")
                        elif status in {"skipped", "insufficient_data"}:
                            msg = f"⚠️ {status}"
                            if reason:
                                msg = f"{msg}: {reason}"
                            st.warning(msg)
                        else:
                            msg = f"❌ {status}"
                            if reason:
                                msg = f"{msg}: {reason}"
                            st.error(msg)

                        # Display detailed training info
                        st.write(f"**Training Summary:**")
                        st.write(
                            f"- Historical data samples: {result.get('historical_data_samples', 0)}")
                        st.write(
                            f"- Historical symbols covered: {result.get('historical_symbols_covered', 0)} / {len(active_watchlist)}")
                        st.write(
                            f"- Personal trade samples: {result.get('personal_trade_samples', 0)}")
                        st.write(
                            f"- Total training samples: {result.get('total_training_samples', 0)}")
                        st.write(
                            f"- LR accuracy: {result.get('lr_accuracy', 0):.1%}")
                        st.write(
                            f"- RF accuracy: {result.get('rf_accuracy', 0):.1%}")
                        st.write(
                            f"- Cache hits: {result.get('historical_cache_hits', 0)}")
                        st.write(
                            f"- Network fetches: {result.get('historical_network_hits', 0)}")
                        st.write(
                            f"- NSE fallback fetches: {result.get('nse_fallback_hits', 0)}")
                        st.write(
                            f"- NSE quote bootstrap: {result.get('nse_quote_fallback_hits', 0)}")
                        if result.get("historical_warning"):
                            st.caption(
                                f"- Warning: {result.get('historical_warning')}")
                        if result.get("label_rebalanced"):
                            st.caption(
                                "- Note: Labels were rebalanced from momentum ranks due single-class historical labels")
                        st.write(f"- Note: {result.get('training_note', '')}")
                    except Exception as e:
                        st.error(f"Training failed: {e}")

    show_ml_scores_table = False
    with col2:
        current_ml_enabled = st.session_state.s_ui_config.get(
            "enable_ml_scoring", False) if isinstance(st.session_state.s_ui_config, dict) else False
        enable_ml_scoring = st.checkbox(
            "Use ML scoring for symbol quality",
            value=current_ml_enabled,
            help="Apply ML-predicted quality scores to influence ranking"
        )
        if enable_ml_scoring != current_ml_enabled:
            st.session_state.s_ui_config["enable_ml_scoring"] = enable_ml_scoring
            _save_state()
        show_ml_scores_table = st.checkbox(
            "Render ML score table (slower)",
            value=False,
            help="Computes per-symbol ML scores and may slow page load on large scan sizes.",
        )

    if (
        get_symbol_quality_score is not None
        and st.session_state.s_ui_config.get("enable_ml_scoring", False)
        and show_ml_scores_table
    ):
        st.subheader("Symbol ML Quality Scores")
        state_file = _state_file()
        state_mtime = float(
            state_file.stat().st_mtime) if state_file.exists() else 0.0
        model_file = resolve_learning_model_path(
            state_file) if resolve_learning_model_path is not None else Path("outputs") / "market_learning_model.pkl"
        model_mtime = float(
            model_file.stat().st_mtime) if model_file.exists() else 0.0
        scores = _batch_ml_scores_cached(
            tuple(active_watchlist), state_mtime, model_mtime)

        if scores:
            score_df = pd.DataFrame(
                [
                    {"Symbol": k, "ML Score (0-1)": v, "Quality": "🟢 High" if v >=
                     0.7 else "🟡 Medium" if v >= 0.5 else "🔴 Low"}
                    for k, v in sorted(scores.items(), key=lambda x: x[1], reverse=True)
                ]
            )
            st.dataframe(score_df, use_container_width=True, hide_index=True)

if actions:
    with st.expander("\U0001f916 Auto-Trade Actions", expanded=True):
        for act in actions:
            st.write(f"- {act}")

if clean_export_err:
    st.warning(clean_export_err)
else:
    st.caption(
        f"Clean closed trades exported: {clean_closed_trade_count} -> {_clean_closed_trades_file()}")

best_action = _best_ai_action(
    buy_df=buy_df,
    sell_df=sell_df,
    sell_exit_df=sell_exit_df,
    market_research=market_research,
    min_buy_score=effective_min_buy_score,
    min_short_score=effective_min_short_score,
    min_sell_score=min_sell_score,
    enable_short_selling=enable_short_selling,
    enable_regime_entry_gate=enable_regime_entry_gate,
)

with st.expander("AI Best Next Action", expanded=True):
    st.write(
        f"Recommendation: {best_action.get('action', 'HOLD')} {best_action.get('symbol', '-')}")
    st.write(
        f"Confidence: {float(best_action.get('confidence', 0.0)):.1f}% | Regime: {market_research.get('regime', 'unknown')}")
    st.caption(str(best_action.get("reason", "")))

optimizer_error = st.session_state.get("s_optimizer_error")
if optimizer_error:
    st.warning(optimizer_error)
if optimizer_summary:
    with st.expander("\U0001f9ea Optimizer Summary", expanded=False):
        st.write(
            f"Status: {optimizer_summary.get('walkforward_status', optimizer_summary.get('model_status', 'unknown'))} | "
            f"Clean closed trades: {optimizer_summary.get('clean_closed_trades', 0)} | "
            f"To 200: {optimizer_summary.get('trades_to_200_goal', 0)} | "
            f"To 300: {optimizer_summary.get('trades_to_300_goal', 0)}"
        )
        st.write(
            f"Baseline net: Rs {float(optimizer_summary.get('baseline_net_pnl', 0.0)):,.2f} | "
            f"Filtered net: Rs {float(optimizer_summary.get('filtered_net_pnl', 0.0)):,.2f}"
        )
        st.write(
            f"Baseline win rate: {float(optimizer_summary.get('baseline_win_rate', 0.0)) * 100.0:.1f}% | "
            f"Filtered win rate: {float(optimizer_summary.get('filtered_win_rate', 0.0)) * 100.0:.1f}%"
        )
        if optimizer_artifacts:
            for k, v in optimizer_artifacts.items():
                st.caption(f"- {k}: {v}")

c1, c2 = st.columns(2)
with c1:
    st.subheader("\U0001f525 Top 5 Buy Signals")
    if buy_df.empty:
        st.info("No buy candidates in selected price band.")
    else:
        show_buy = buy_df[[
            "symbol",
            "price",
            "rank_qty",
            "rank_order_value",
            "rank_expected_profit",
            "rank_symbol_headroom",
            "rank_deployment_headroom",
            "buy_score",
            "research_buy_score",
            "research_tag",
            "effective_buy_score",
            "buy_signal",
            "pchange",
            "updated",
        ]].copy()
        show_buy["price"] = show_buy["price"].map(
            lambda x: f"Rs {float(x):.2f}")
        show_buy["rank_order_value"] = show_buy["rank_order_value"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_buy["rank_expected_profit"] = show_buy["rank_expected_profit"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_buy["rank_symbol_headroom"] = show_buy["rank_symbol_headroom"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_buy["rank_deployment_headroom"] = show_buy["rank_deployment_headroom"].map(
            lambda x: f"Rs {float(x):,.0f}")
        st.dataframe(show_buy, use_container_width=True, hide_index=True)

with c2:
    st.subheader("\U0001f9ca Top 5 Sell Signals")
    if sell_df.empty:
        st.info("No sell candidates in selected price band.")
    else:
        show_sell = sell_df[[
            "symbol",
            "price",
            "rank_qty",
            "rank_order_value",
            "rank_expected_profit",
            "rank_symbol_headroom",
            "rank_deployment_headroom",
            "sell_score",
            "research_sell_score",
            "research_tag",
            "effective_sell_score",
            "sell_signal",
            "pchange",
            "updated",
        ]].copy()
        show_sell["price"] = show_sell["price"].map(
            lambda x: f"Rs {float(x):.2f}")
        show_sell["rank_order_value"] = show_sell["rank_order_value"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_sell["rank_expected_profit"] = show_sell["rank_expected_profit"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_sell["rank_symbol_headroom"] = show_sell["rank_symbol_headroom"].map(
            lambda x: f"Rs {float(x):,.0f}")
        show_sell["rank_deployment_headroom"] = show_sell["rank_deployment_headroom"].map(
            lambda x: f"Rs {float(x):,.0f}")
        st.dataframe(show_sell, use_container_width=True, hide_index=True)

st.subheader("\U0001f4c8 Open Positions")
if holdings_df.empty:
    st.info("No open positions")
else:
    view_h = holdings_df.copy()
    preferred_cols = ["side", "symbol", "qty", "avg", "ltp",
                      "stop", "target", "invested", "pnl", "pnl_pct"]
    present_cols = [c for c in preferred_cols if c in view_h.columns]
    if present_cols:
        view_h = view_h[present_cols]
    for col in ["avg", "ltp", "stop", "target", "invested", "pnl"]:
        view_h[col] = view_h[col].map(lambda x: f"Rs {float(x):.2f}")
    if "pnl_pct" in view_h.columns:
        view_h["pnl_pct"] = view_h["pnl_pct"].map(lambda x: f"{float(x):.2f}%")
    st.dataframe(view_h, use_container_width=True, hide_index=True)

st.subheader("\U0001f4d2 Trade History")
log_df = pd.DataFrame(st.session_state.s_log)
if log_df.empty:
    st.info("No trades yet")
else:
    view_log = log_df.sort_values("ts", ascending=False).copy()
    for col in ["price", "charges", "cash_after"]:
        if col in view_log.columns:
            view_log[col] = view_log[col].map(lambda x: f"Rs {float(x):.2f}")
    st.dataframe(view_log, use_container_width=True, hide_index=True)

if scan_errors:
    with st.expander("Scan errors", expanded=False):
        for err in scan_errors[:10]:
            st.caption(err)

if auto_refresh_on:
    st.caption(f"Auto refresh active: every {refresh_seconds}s")
    _auto_refresh(refresh_seconds)
