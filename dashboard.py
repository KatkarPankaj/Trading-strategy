"""
Intraday Strategy Dashboard — NSE/BSE
Run with: streamlit run dashboard.py
"""

import streamlit.components.v1 as components
import streamlit as st
import pandas as pd
from typing import Any
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import json
import time as pytime
import sys
from stockmarket.config import TradingConfig
from stockmarket.data import _cache_path, fetch_intraday_data
from stockmarket.strategy import add_strategy_columns
from stockmarket.core import MarketSession


sys.path.insert(0, str(Path(__file__).parent / "src"))


try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None


DEFAULT_SESSION = MarketSession.from_config(TradingConfig())

WATCHLIST = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS",
    "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "BAJFINANCE.NS",
    "WIPRO.NS", "LTIM.NS", "BAJAJ-AUTO.NS", "TATAMOTORS.NS",
    "MARUTI.NS", "SUNPHARMA.NS", "HINDUNILVR.NS",
    "PERSISTENT.NS", "COFORGE.NS", "DIXON.NS", "POLYCAB.NS",
    "IRCTC.NS", "IEX.NS", "CDSL.NS", "BSE.NS",
    "MOTHERSON.NS", "TATAPOWER.NS", "BEL.NS", "BHEL.NS",
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "AUROPHARMA.NS",
]

SECTOR_MAP = {
    "RELIANCE.NS": "Energy",
    "TCS.NS": "IT",
    "INFY.NS": "IT",
    "WIPRO.NS": "IT",
    "LTIM.NS": "IT",
    "HDFCBANK.NS": "Banking",
    "ICICIBANK.NS": "Banking",
    "SBIN.NS": "Banking",
    "AXISBANK.NS": "Banking",
    "BAJFINANCE.NS": "Financial Services",
    "BAJAJ-AUTO.NS": "Auto",
    "TATAMOTORS.NS": "Auto",
    "MARUTI.NS": "Auto",
    "SUNPHARMA.NS": "Pharma",
    "HINDUNILVR.NS": "FMCG",
    "PERSISTENT.NS": "IT",
    "COFORGE.NS": "IT",
    "DIXON.NS": "Consumer Durables",
    "POLYCAB.NS": "Electrical",
    "IRCTC.NS": "Railways",
    "IEX.NS": "Exchange",
    "CDSL.NS": "Financial Services",
    "BSE.NS": "Exchange",
    "MOTHERSON.NS": "Auto Ancillaries",
    "TATAPOWER.NS": "Power",
    "BEL.NS": "Defence",
    "BHEL.NS": "Capital Goods",
    "FEDERALBNK.NS": "Banking",
    "IDFCFIRSTB.NS": "Banking",
    "AUROPHARMA.NS": "Pharma",
}

CAP_BUCKET_MAP = {
    "RELIANCE.NS": "Large",
    "TCS.NS": "Large",
    "INFY.NS": "Large",
    "HDFCBANK.NS": "Large",
    "ICICIBANK.NS": "Large",
    "SBIN.NS": "Large",
    "AXISBANK.NS": "Large",
    "BAJFINANCE.NS": "Large",
    "WIPRO.NS": "Large",
    "LTIM.NS": "Mid",
    "BAJAJ-AUTO.NS": "Large",
    "TATAMOTORS.NS": "Large",
    "MARUTI.NS": "Large",
    "SUNPHARMA.NS": "Large",
    "HINDUNILVR.NS": "Large",
    "PERSISTENT.NS": "Mid",
    "COFORGE.NS": "Mid",
    "DIXON.NS": "Mid",
    "POLYCAB.NS": "Large",
    "IRCTC.NS": "Mid",
    "IEX.NS": "Small",
    "CDSL.NS": "Mid",
    "BSE.NS": "Small",
    "MOTHERSON.NS": "Mid",
    "TATAPOWER.NS": "Large",
    "BEL.NS": "Large",
    "BHEL.NS": "Mid",
    "FEDERALBNK.NS": "Mid",
    "IDFCFIRSTB.NS": "Small",
    "AUROPHARMA.NS": "Mid",
}

PAPER_STATE_FILE = Path("outputs") / "paper_state.json"
TRADE_HISTORY_FILE = Path("outputs") / "paper_trade_history.csv"
DAILY_PNL_FILE = Path("outputs") / "daily_pnl_history.csv"
INTRADAY_MARGIN_RATE = 0.20
MAX_POSITION_QTY_PER_STOCK = 200
MAX_BUY_NOTIONAL_PER_STOCK = 50000.0


def ist_now() -> datetime:
    return DEFAULT_SESSION.now()


def market_now(tz_name: str) -> datetime:
    return MarketSession.from_config(
        TradingConfig(market_timezone=tz_name)
    ).now()


def format_market_timestamp(ts: pd.Timestamp | datetime, tz_name: str) -> str:
    dt = pd.Timestamp(ts)
    if dt.tzinfo is None:
        dt = dt.tz_localize(ZoneInfo(tz_name))
    else:
        dt = dt.tz_convert(ZoneInfo(tz_name))
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def market_phase(
    at: datetime, session: MarketSession
) -> tuple[str, str, str]:
    if session.is_before_open(at):
        return "Pre-Market", "Prepare watchlist. Do not enter trades yet.", "#6c757d"
    if session.is_opening_range(at):
        return (
            "Opening Range Formation",
            f"Observe {session.market_open:%H:%M}-{session.opening_range_end:%H:%M} opening range. No entries yet.",
            "#fd7e14",
        )
    if session.is_entry_allowed(at):
        return "Active Trading Window", "Primary entry window for intraday setups is open.", "#198754"
    if session.is_late_session(at):
        return "Late Session", "Avoid fresh entries. Manage open trades.", "#ffc107"
    if session.is_square_off(at) and not session.is_market_closed(at):
        return "Square-Off Zone", "Close all intraday positions.", "#dc3545"
    return "Market Closed", "Review session and prepare next day plan.", "#6c757d"


def load_config(path: str = "config.json") -> TradingConfig:
    try:
        return TradingConfig.from_json(path)
    except Exception:
        return TradingConfig()


def format_pct(val: float) -> str:
    return f"{val * 100:.2f}%"


def _parse_time(value: str, fallback: time) -> time:
    try:
        return time.fromisoformat(value)
    except Exception:
        return fallback


def _auto_refresh(seconds: int, hard_reload_fallback: bool = False) -> None:
    if seconds <= 0:
        return
    components.html(
        f"""
                <script>
                    (function() {{
                        const delayMs = {int(seconds) * 1000};
                        setTimeout(function() {{
                            console.log('[Auto-Refresh] Triggering rerun after {seconds}s...');

                            // Preferred: trigger Streamlit script rerun (soft refresh).
                            try {{
                                window.parent.postMessage({{isStreamlitMessage: true, type: "streamlit:rerunScript"}}, "*");
                                return;
                            }} catch (e) {{
                                console.warn('[Auto-Refresh] Rerun message failed:', e);
                            }}

                            // Optional fallback: hard reload only if explicitly enabled.
                            const hardReloadFallback = {str(hard_reload_fallback).lower()};
                            if (!hardReloadFallback) {{
                                return;
                            }}

                            try {{
                                if (window.parent && window.parent !== window && window.parent.location) {{
                                    window.parent.location.reload();
                                    return;
                                }}
                            }} catch (e) {{
                                console.warn('[Auto-Refresh] Parent hard reload failed:', e);
                            }}

                            try {{
                                window.location.reload();
                            }} catch (e) {{
                                console.warn('[Auto-Refresh] Hard reload failed:', e);
                            }}
                        }}, delayMs);

                        console.log('[Auto-Refresh] Timer set for {seconds} seconds');
                    }})();
                </script>
                """,
        height=0,
        width=0,
    )


def _in_entry_window(cfg: TradingConfig) -> bool:
    session = MarketSession.from_config(cfg)
    return session.is_entry_allowed(session.now())


def _in_square_off_window(cfg: TradingConfig) -> bool:
    session = MarketSession.from_config(cfg)
    return session.is_square_off(session.now())


def _in_auto_exit_window(cfg: TradingConfig) -> bool:
    session = MarketSession.from_config(cfg)
    return session.is_auto_exit_window(session.now(), minutes_before_close=10)


def _today_buy_count() -> int:
    today = ist_now().strftime("%Y-%m-%d")
    return sum(
        1
        for row in st.session_state.get("paper_trade_log", [])
        if str(row.get("timestamp_ist", "")).startswith(today)
        and str(row.get("side", "")).upper() == "BUY"
    )


def _scan_target_count(
    cfg: TradingConfig,
    base_top_n: int,
    expanded_top_n: int,
    expansion_review_time: time,
    enable_expansion: bool,
) -> tuple[int, bool]:
    if not enable_expansion:
        return base_top_n, False

    session = MarketSession.from_config(cfg)
    expansion_active = (
        session.is_between_local_times(
            session.now(), expansion_review_time, session.entry_cutoff
        )
        and _today_buy_count() == 0
    )
    return (expanded_top_n if expansion_active else base_top_n), expansion_active


def _apply_scan_expansion_filter(
    ranked_df: pd.DataFrame,
    base_top_n: int,
    expanded_top_n: int,
    extra_min_score: float,
    expansion_active: bool,
) -> pd.DataFrame:
    if ranked_df.empty:
        return ranked_df
    if not expansion_active:
        # Keep full ranked pool so downstream diversity filters can still backfill Top-N.
        return ranked_df.reset_index(drop=True)

    base_df = ranked_df.head(base_top_n)
    # Consider all remaining rows for expansion eligibility.
    extra_df = ranked_df.iloc[base_top_n:].copy()
    if not extra_df.empty:
        extra_df = extra_df[extra_df["score"].astype(
            float) >= float(extra_min_score)]
    return pd.concat([base_df, extra_df], ignore_index=True)


def _sector_for_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    return SECTOR_MAP.get(sym, "Other")


def _apply_sector_diversity_cap(
    ranked_df: pd.DataFrame,
    max_per_sector: int,
    top_n: int,
) -> pd.DataFrame:
    """Limit concentration so one sector does not dominate Top-N."""
    if ranked_df.empty:
        return ranked_df

    top_n = max(1, int(top_n))
    max_per_sector = max(1, int(max_per_sector))

    kept_rows: list[dict[str, Any]] = []
    sector_counts: dict[str, int] = {}

    for _, row in ranked_df.iterrows():
        if len(kept_rows) >= top_n:
            break
        sector = _sector_for_symbol(str(row.get("symbol", "")))
        if int(sector_counts.get(sector, 0)) >= max_per_sector:
            continue
        kept_rows.append(row.to_dict())
        sector_counts[sector] = int(sector_counts.get(sector, 0)) + 1

    out = pd.DataFrame(kept_rows)
    if out.empty:
        return ranked_df.head(top_n).reset_index(drop=True)
    return out.reset_index(drop=True)


def _cap_bucket_for_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    return CAP_BUCKET_MAP.get(sym, "Mid")


def _apply_cap_focus_filter(ranked_df: pd.DataFrame, ignore_large_cap: bool) -> pd.DataFrame:
    """Optionally exclude large-cap symbols from candidate pool."""
    if ranked_df.empty or not bool(ignore_large_cap):
        return ranked_df
    filtered = ranked_df[
        ranked_df["symbol"].map(
            lambda s: _cap_bucket_for_symbol(str(s)) != "Large")
    ].copy()
    return filtered.reset_index(drop=True)


def _apply_price_band_filter(
    ranked_df: pd.DataFrame,
    min_price: float,
    max_price: float,
) -> pd.DataFrame:
    """Keep only symbols within the configured tradable price band."""
    if ranked_df.empty:
        return ranked_df
    lo = float(max(0.0, min_price))
    hi = float(max(lo, max_price))
    out = ranked_df.copy()
    out = out[(out["price"].astype(float) > 0.0) &
              (out["price"].astype(float) >= lo) &
              (out["price"].astype(float) <= hi)]
    return out.reset_index(drop=True)


def _apply_market_cap_quota(
    ranked_df: pd.DataFrame,
    top_n: int,
    enable_quota: bool,
    large_quota: int,
    mid_quota: int,
    small_quota: int,
) -> pd.DataFrame:
    """Diversify Top-N across market-cap buckets with graceful backfill."""
    if ranked_df.empty:
        return ranked_df

    top_n = max(1, int(top_n))
    if not enable_quota:
        return ranked_df.head(top_n).reset_index(drop=True)

    quotas = {
        "Large": max(0, int(large_quota)),
        "Mid": max(0, int(mid_quota)),
        "Small": max(0, int(small_quota)),
    }

    selected_rows: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    used = {"Large": 0, "Mid": 0, "Small": 0}

    # Pass 1: take rows that satisfy bucket quotas in ranked order.
    for _, row in ranked_df.iterrows():
        if len(selected_rows) >= top_n:
            break
        sym = str(row.get("symbol", ""))
        if sym in selected_symbols:
            continue
        bucket = _cap_bucket_for_symbol(sym)
        if bucket in quotas and used.get(bucket, 0) < quotas[bucket]:
            selected_rows.append(row.to_dict())
            selected_symbols.add(sym)
            used[bucket] = int(used.get(bucket, 0)) + 1

    # Pass 2: backfill remaining slots from best-ranked leftovers.
    if len(selected_rows) < top_n:
        for _, row in ranked_df.iterrows():
            if len(selected_rows) >= top_n:
                break
            sym = str(row.get("symbol", ""))
            if sym in selected_symbols:
                continue
            selected_rows.append(row.to_dict())
            selected_symbols.add(sym)

    out = pd.DataFrame(selected_rows)
    if out.empty:
        return ranked_df.head(top_n).reset_index(drop=True)
    return out.reset_index(drop=True)


def _strategy_plan_from_yahoo(last_row: pd.Series, cfg: TradingConfig, strategy_mode: str) -> dict[str, Any]:
    close = float(last_row["close"])
    vwap = float(last_row["vwap"])
    or_high = float(last_row["or_high"])
    or_low = float(last_row["or_low"])
    vol_spike = bool(last_row["vol_spike"])

    plan = {
        "side": "BUY",
        "entry_price": None,
        "trigger_hit": False,
        "reason": "Waiting for strategy conditions",
        "sl": None,
        "tp": None,
    }

    in_window = _in_entry_window(cfg)
    if strategy_mode == "ORB + VWAP":
        expected = or_high
        trigger = in_window and vol_spike and (
            close >= expected) and (close > vwap)
        reason = "Need breakout above OR high with VWAP support and volume spike"
    elif strategy_mode == "VWAP Trend":
        expected = max(vwap, float(last_row["open"]))
        trigger = in_window and vol_spike and (
            close >= expected) and (close > vwap)
        reason = "Need trend continuation above VWAP with volume spike"
    else:
        expected = or_low * 1.002
        near_low = close <= expected
        trigger = in_window and near_low and (close > vwap)
        reason = "Need reversal near OR low with VWAP recovery"

    plan["entry_price"] = expected
    plan["trigger_hit"] = trigger
    plan["reason"] = reason if not trigger else "Entry trigger reached"
    plan["sl"] = expected * (1 - cfg.stop_loss_pct)
    plan["tp"] = expected * (1 + cfg.take_profit_pct)
    return plan


def _strategy_plan_from_nse_quote(quote: dict[str, float], cfg: TradingConfig, strategy_mode: str) -> dict[str, Any]:
    price = float(quote["last_price"])
    vwap = float(quote["vwap"])
    chg = float(quote["pchange"])

    plan = {
        "side": "BUY",
        "entry_price": None,
        "trigger_hit": False,
        "reason": "Waiting for quote-based strategy conditions",
        "sl": None,
        "tp": None,
    }

    in_window = _in_entry_window(cfg)
    if strategy_mode == "VWAP Trend":
        expected = vwap if vwap > 0 else price
        trigger = in_window and (vwap > 0) and (
            price >= expected) and (chg > 0.3)
        reason = "Need price above VWAP with positive momentum"
    elif strategy_mode == "OR Reversal":
        expected = price
        trigger = in_window and (chg < -0.8) and (vwap == 0 or price > vwap)
        reason = "Need oversold reversal confirmation"
    else:
        # NSE-only fast path: avoid Yahoo bar fetches in live quote mode.
        expected = vwap if vwap > 0 else price
        trigger = in_window and (vwap > 0) and (
            price >= expected) and (chg > 0.4)
        reason = "Need bullish momentum above VWAP"

    plan["entry_price"] = expected
    plan["trigger_hit"] = trigger
    plan["reason"] = reason if not trigger else "Entry trigger reached"
    plan["sl"] = expected * (1 - cfg.stop_loss_pct)
    plan["tp"] = expected * (1 + cfg.take_profit_pct)
    return plan


def _init_strategy_tracking() -> None:
    if "strategy_tracking" not in st.session_state:
        st.session_state.strategy_tracking = {}


def _update_strategy_tracking(
    symbol: str,
    strategy_mode: str,
    plan: dict[str, Any],
    current_price: float,
    tz_name: str,
) -> None:
    expected = float(plan["entry_price"]) if plan.get("entry_price") else 0.0
    if expected <= 0:
        benefit_pct = 0.0
    else:
        benefit_pct = ((current_price - expected) / expected) * 100.0

    st.session_state.strategy_tracking[symbol] = {
        "symbol": symbol,
        "strategy": strategy_mode,
        "expected_entry": expected,
        "current_price": float(current_price),
        "trigger": "READY" if bool(plan.get("trigger_hit")) else "WAIT",
        "benefit_pct": round(benefit_pct, 2),
        "sl": float(plan.get("sl") or 0.0),
        "tp": float(plan.get("tp") or 0.0),
        "updated": market_now(tz_name).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _reset_paper_state(starting_cash: float) -> None:
    PAPER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    st.session_state.paper_cash = float(starting_cash)
    st.session_state.paper_starting_cash = float(starting_cash)
    st.session_state.paper_realized_pnl = 0.0
    st.session_state.paper_total_charges = 0.0
    st.session_state.paper_holdings = {}
    st.session_state.paper_trade_log = []
    st.session_state.paper_prices = {}
    st.session_state.paper_price_updates = {}
    st.session_state.paper_pending_orders = []
    st.session_state.paper_armed_symbols = []
    _save_paper_state()


def _init_paper_state(starting_cash: float) -> None:
    if "paper_cash" not in st.session_state:
        _load_paper_state(starting_cash)


def _load_paper_state(starting_cash: float) -> None:
    if not PAPER_STATE_FILE.exists():
        _reset_paper_state(starting_cash)
        return

    try:
        data = json.loads(PAPER_STATE_FILE.read_text(encoding="utf-8"))
        st.session_state.paper_cash = float(
            data.get("paper_cash", starting_cash))
        st.session_state.paper_starting_cash = float(
            data.get("paper_starting_cash", starting_cash))
        st.session_state.paper_realized_pnl = float(
            data.get("paper_realized_pnl", 0.0))
        st.session_state.paper_total_charges = float(
            data.get("paper_total_charges", 0.0))
        st.session_state.paper_holdings = data.get("paper_holdings", {})
        st.session_state.paper_trade_log = data.get("paper_trade_log", [])
        st.session_state.paper_prices = data.get("paper_prices", {})
        st.session_state.paper_price_updates = data.get(
            "paper_price_updates", {})
        st.session_state.paper_pending_orders = data.get(
            "paper_pending_orders", [])
        st.session_state.paper_armed_symbols = data.get(
            "paper_armed_symbols", [])
        st.session_state.auto_tune_quality = bool(
            data.get("auto_tune_quality", True)
        )
        tune_mode = str(data.get("tune_mode", "Balanced"))
        st.session_state.tune_mode = (
            tune_mode if tune_mode in ["Conservative",
                                       "Balanced", "Aggressive"] else "Balanced"
        )
    except Exception:
        _reset_paper_state(starting_cash)


def _save_paper_state() -> None:
    payload = {
        "paper_cash": float(st.session_state.paper_cash),
        "paper_starting_cash": float(st.session_state.paper_starting_cash),
        "paper_realized_pnl": float(st.session_state.paper_realized_pnl),
        "paper_total_charges": float(st.session_state.paper_total_charges),
        "paper_holdings": st.session_state.paper_holdings,
        "paper_trade_log": st.session_state.paper_trade_log,
        "paper_prices": st.session_state.paper_prices,
        "paper_price_updates": st.session_state.paper_price_updates,
        "paper_pending_orders": st.session_state.get("paper_pending_orders", []),
        "paper_armed_symbols": st.session_state.get("paper_armed_symbols", []),
        "auto_tune_quality": bool(st.session_state.get("auto_tune_quality", True)),
        "tune_mode": str(st.session_state.get("tune_mode", "Balanced")),
    }
    PAPER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAPER_STATE_FILE.write_text(json.dumps(
        payload, indent=2), encoding="utf-8")


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _pending_buy_orders() -> list[dict[str, Any]]:
    orders = st.session_state.get("paper_pending_orders", [])
    return [o for o in orders if str(o.get("side", "BUY")).upper() == "BUY"]


def _armed_symbols() -> list[dict[str, Any]]:
    return list(st.session_state.get("paper_armed_symbols", []))


def _ensure_auto_trade_state() -> None:
    if "paper_pending_orders" not in st.session_state:
        st.session_state.paper_pending_orders = []
    if "paper_armed_symbols" not in st.session_state:
        st.session_state.paper_armed_symbols = []


def _add_pending_buy_order(symbol: str, target_price: float, qty: int, strategy_mode: str) -> None:
    if qty <= 0:
        raise ValueError("Quantity must be greater than zero")
    if target_price <= 0:
        raise ValueError("Target price must be greater than zero")

    normalized_symbol = _normalize_symbol(symbol)
    orders = [
        order for order in _pending_buy_orders()
        if _normalize_symbol(str(order.get("symbol", ""))) != normalized_symbol
    ]
    orders.append(
        {
            "symbol": normalized_symbol,
            "side": "BUY",
            "target_price": float(target_price),
            "qty": int(qty),
            "strategy": strategy_mode,
            "created_at": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    st.session_state.paper_pending_orders = orders
    _save_paper_state()


def _remove_pending_buy_order(symbol: str) -> None:
    normalized_symbol = _normalize_symbol(symbol)
    st.session_state.paper_pending_orders = [
        order for order in _pending_buy_orders()
        if _normalize_symbol(str(order.get("symbol", ""))) != normalized_symbol
    ]
    _save_paper_state()


def _arm_symbol(symbol: str, strategy_mode: str) -> None:
    normalized_symbol = _normalize_symbol(symbol)
    armed = [
        item for item in _armed_symbols()
        if _normalize_symbol(str(item.get("symbol", ""))) != normalized_symbol
    ]
    armed.append(
        {
            "symbol": normalized_symbol,
            "strategy": strategy_mode,
            "armed_at": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    st.session_state.paper_armed_symbols = armed
    _save_paper_state()


def _disarm_symbol(symbol: str) -> None:
    normalized_symbol = _normalize_symbol(symbol)
    st.session_state.paper_armed_symbols = [
        item for item in _armed_symbols()
        if _normalize_symbol(str(item.get("symbol", ""))) != normalized_symbol
    ]
    _save_paper_state()


def _fetch_live_plan(
    symbol: str,
    cfg: TradingConfig,
    strategy_mode: str,
    data_source: str,
    fallback_price: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    price = float(fallback_price)
    if data_source == "NSE Quote API (non-Yahoo)":
        quote = fetch_nse_quote(symbol)
        price = float(quote.get("last_price") or fallback_price)
        plan = _strategy_plan_from_nse_quote(
            quote=quote,
            cfg=cfg,
            strategy_mode=strategy_mode,
        )
    else:
        df = fetch_intraday_data(
            symbol,
            cfg.interval,
            cfg.period,
            tz=cfg.market_timezone,
            session=MarketSession.from_config(cfg),
            max_retries=1,
            backoff_base=1.0,
        )
        if df.empty:
            raise ValueError(f"No intraday data available for {symbol}")
        sdf = add_strategy_columns(df, cfg)
        last_row = sdf.iloc[-1]
        price = float(last_row["close"])
        plan = _strategy_plan_from_yahoo(
            last_row=last_row,
            cfg=cfg,
            strategy_mode=strategy_mode,
        )

    if price > 0:
        _update_paper_price(symbol, price)
    return price, plan


def _append_trade_history_row(trade_row: dict[str, Any]) -> None:
    TRADE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([trade_row]).to_csv(
        TRADE_HISTORY_FILE,
        mode="a",
        header=not TRADE_HISTORY_FILE.exists(),
        index=False,
    )


@st.cache_data(ttl=5, show_spinner=False)
def _trade_history_df_cached(rows_json: str) -> pd.DataFrame:
    if not rows_json:
        return pd.DataFrame()
    try:
        data = json.loads(rows_json)
    except Exception:
        return pd.DataFrame()
    if not isinstance(data, list):
        return pd.DataFrame()
    return pd.DataFrame(data)


def _trade_history_df() -> pd.DataFrame:
    rows = st.session_state.get("paper_trade_log", [])
    if not rows:
        return pd.DataFrame()
    try:
        rows_json = json.dumps(rows, default=str)
    except Exception:
        return pd.DataFrame(rows)
    return _trade_history_df_cached(rows_json)


def _load_saved_ui_preferences() -> dict[str, Any]:
    """Read persisted UI preferences from paper state file for hard-refresh recovery."""
    if not PAPER_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(PAPER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "auto_tune_quality": bool(data.get("auto_tune_quality", True)),
        "tune_mode": str(data.get("tune_mode", "Balanced")),
    }


def _open_position_entry_charges() -> dict[str, float]:
    """Return entry-side charges for currently open positions by symbol."""
    rows = st.session_state.get("paper_trade_log", [])
    if not rows:
        return {}

    entry_charges: dict[str, float] = {}
    for row in rows:
        sym = str(row.get("symbol", ""))
        side = str(row.get("side", "")).upper()
        charge = float(row.get("charges", 0.0) or 0.0)
        if side == "BUY":
            entry_charges[sym] = charge
        elif side == "SELL":
            entry_charges.pop(sym, None)
    return entry_charges


def _learning_summary(history_df: pd.DataFrame) -> list[str]:
    if history_df.empty:
        return ["No trade history yet."]

    closed = history_df[history_df["side"] == "SELL"].copy()
    if closed.empty:
        return ["No closed trades yet. Learning starts after exits."]

    closed_realized = closed["realized_pnl"].astype(float)
    wins = closed[closed_realized > 0]
    losses = closed[closed_realized < 0]

    win_rate = float((closed_realized > 0).mean())
    avg_win = float(wins["realized_pnl"].astype(
        float).mean()) if not wins.empty else 0.0
    avg_loss = float(losses["realized_pnl"].astype(
        float).mean()) if not losses.empty else 0.0

    charges_total = float(history_df.get(
        "charges", pd.Series(dtype=float)).astype(float).sum())
    gross_realized = float(closed_realized.sum())

    tips: list[str] = []
    tips.append(
        f"Closed trades: {len(closed)} | Win rate: {win_rate * 100:.2f}%")
    tips.append(f"Avg win: Rs {avg_win:.2f} | Avg loss: Rs {avg_loss:.2f}")
    tips.append(
        f"Gross realized PnL: Rs {gross_realized:.2f} | Total charges: Rs {charges_total:.2f}")

    if win_rate < 0.45:
        tips.append(
            "Learning: low win rate; tighten entry trigger conditions and avoid marginal setups.")
    if avg_loss < 0 and abs(avg_loss) > abs(avg_win):
        tips.append(
            "Learning: losses are larger than wins; reduce stop loss distance or exit faster on weak momentum.")
    if abs(gross_realized) > 0 and charges_total > abs(gross_realized) * 0.2:
        tips.append(
            "Learning: charges are high relative to PnL; reduce over-trading and focus on higher-conviction entries.")

    by_symbol = closed.groupby("symbol", as_index=False)[
        "realized_pnl"].sum().sort_values("realized_pnl", ascending=False)
    if not by_symbol.empty:
        best = by_symbol.iloc[0]
        worst = by_symbol.iloc[-1]
        tips.append(
            f"Best symbol so far: {best['symbol']} (Rs {float(best['realized_pnl']):.2f})")
        tips.append(
            f"Worst symbol so far: {worst['symbol']} (Rs {float(worst['realized_pnl']):.2f})")

    return tips


def _export_daily_pnl(history_df: pd.DataFrame) -> None:
    """Upsert today's aggregated PnL row into daily_pnl_history.csv."""
    if history_df.empty or "timestamp_ist" not in history_df.columns:
        return
    today = ist_now().strftime("%Y-%m-%d")
    df = history_df.copy()
    df["trade_date"] = df["timestamp_ist"].astype(str).str.slice(0, 10)
    for col in ["realized_pnl", "charges"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = df[col].astype(float)
    today_rows = df[df["trade_date"] == today]
    if today_rows.empty:
        return
    realized = float(today_rows["realized_pnl"].sum())
    charges = float(today_rows["charges"].sum())
    trades = int(len(today_rows))
    net_pnl = realized - charges
    new_row = pd.DataFrame([{
        "trade_date": today,
        "realized_pnl": round(realized, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net_pnl, 2),
        "trades": trades,
    }])
    DAILY_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DAILY_PNL_FILE.exists():
        existing = pd.read_csv(DAILY_PNL_FILE, dtype=str)
        existing = existing[existing["trade_date"] != today]
        updated = pd.concat([existing, new_row.astype(str)], ignore_index=True)
    else:
        updated = new_row.astype(str)
    updated.to_csv(DAILY_PNL_FILE, index=False)


def _strategy_learning_summary(history_df: pd.DataFrame) -> pd.DataFrame:
    """Return per-strategy win-rate and avg PnL for SELL trades."""
    if history_df.empty or "note" not in history_df.columns:
        return pd.DataFrame()
    closed = history_df[history_df["side"] == "SELL"].copy()
    if closed.empty:
        return pd.DataFrame()
    import re

    def _extract_strategy(note: str) -> str:
        m = re.search(r"\(([^)]+)\)", str(note))
        return m.group(1) if m else "Unknown"
    closed["strategy"] = closed["note"].apply(_extract_strategy)
    closed["realized_pnl"] = closed["realized_pnl"].astype(float)
    closed["charges"] = closed["charges"].astype(float)

    def _agg(g: pd.DataFrame) -> pd.Series:
        wins = (g["realized_pnl"] > 0).sum()
        total = len(g)
        return pd.Series({
            "trades": total,
            "win_rate_%": round(wins / total * 100, 1) if total else 0.0,
            "avg_pnl_rs": round(float(g["realized_pnl"].mean()), 2),
            "total_pnl_rs": round(float(g["realized_pnl"].sum()), 2),
            "total_charges_rs": round(float(g["charges"].sum()), 2),
        })
    result = closed.groupby(
        "strategy", as_index=True).apply(_agg).reset_index()
    result = result.sort_values("total_pnl_rs", ascending=False)
    return result


def _learning_symbol_profile(history_df: pd.DataFrame, strategy_mode: str) -> pd.DataFrame:
    """Build a lightweight learning profile by symbol from closed trades."""
    if history_df.empty or "side" not in history_df.columns:
        return pd.DataFrame()

    closed = history_df[history_df["side"] == "SELL"].copy()
    if closed.empty:
        return pd.DataFrame()

    import re

    def _extract_strategy(note: str) -> str:
        m = re.search(r"\(([^)]+)\)", str(note))
        return m.group(1) if m else "Unknown"

    closed["strategy"] = closed.get(
        "note", "").astype(str).map(_extract_strategy)
    closed["realized_pnl"] = closed["realized_pnl"].astype(float)

    strat_rows = closed[closed["strategy"] == str(strategy_mode)]
    use_df = strat_rows if len(strat_rows) >= 3 else closed

    profile = use_df.groupby("symbol", as_index=False).agg(
        trades=("symbol", "count"),
        win_rate=("realized_pnl", lambda s: float((s > 0).mean())),
        avg_pnl=("realized_pnl", "mean"),
        total_pnl=("realized_pnl", "sum"),
    )

    profile["win_rate"] = profile["win_rate"].astype(float)
    profile["avg_pnl"] = profile["avg_pnl"].astype(float)
    profile["total_pnl"] = profile["total_pnl"].astype(float)

    # Lightweight "learning agent" score from outcomes.
    profile["learning_bias"] = (
        (profile["win_rate"] - 0.5) * 20.0
        + profile["avg_pnl"].clip(lower=-250.0, upper=250.0) / 30.0
    )
    low_conf = profile["trades"] < 2
    profile.loc[low_conf, "learning_bias"] = profile.loc[low_conf,
                                                         "learning_bias"] * 0.6
    profile["learning_bias"] = profile["learning_bias"].round(2)

    return profile.sort_values("learning_bias", ascending=False).reset_index(drop=True)


def _apply_learning_bonus(ranked_df: pd.DataFrame, history_df: pd.DataFrame, strategy_mode: str) -> pd.DataFrame:
    """Re-rank candidates with a small learning bonus from historical outcomes."""
    if ranked_df.empty:
        return ranked_df

    profile = _learning_symbol_profile(history_df, strategy_mode)
    if profile.empty or "symbol" not in ranked_df.columns:
        out = ranked_df.copy()
        out["learning_bonus"] = 0.0
        return out

    bias_map = dict(zip(profile["symbol"], profile["learning_bias"]))
    out = ranked_df.copy()
    out["learning_bonus"] = out["symbol"].map(
        lambda s: float(bias_map.get(str(s), 0.0))).astype(float)
    out["score"] = (out["score"].astype(float) +
                    out["learning_bonus"].clip(lower=-8.0, upper=8.0)).round(2)
    out = out.sort_values(by=["score", "range_pct", "price"], ascending=[
        False, False, False]).reset_index(drop=True)
    return out


def _recent_daily_learning_stats(lookback_days: int = 5) -> pd.DataFrame:
    """Build daily outcome stats from persisted history for threshold auto-tuning."""
    if not TRADE_HISTORY_FILE.exists():
        return pd.DataFrame()

    try:
        h = pd.read_csv(TRADE_HISTORY_FILE)
    except Exception:
        return pd.DataFrame()

    required = {"timestamp_ist", "side", "realized_pnl", "charges"}
    if h.empty or not required.issubset(set(h.columns)):
        return pd.DataFrame()

    h = h.copy()
    h["trade_date"] = h["timestamp_ist"].astype(str).str.slice(0, 10)
    h["realized_pnl"] = pd.to_numeric(
        h["realized_pnl"], errors="coerce").fillna(0.0)
    h["charges"] = pd.to_numeric(h["charges"], errors="coerce").fillna(0.0)

    daily_rows: list[dict[str, Any]] = []
    for day, g in h.groupby("trade_date"):
        closed = g[g["side"].astype(str).str.upper() == "SELL"]
        if closed.empty:
            continue

        gross_realized = float(closed["realized_pnl"].sum())
        total_charges = float(g["charges"].sum())
        closed_count = int(len(closed))
        wins = int((closed["realized_pnl"] > 0).sum())

        daily_rows.append(
            {
                "trade_date": day,
                "closed_trades": closed_count,
                "win_rate": float(wins / closed_count) if closed_count > 0 else 0.0,
                "gross_realized": gross_realized,
                "charges": total_charges,
                "net_pnl": gross_realized - total_charges,
            }
        )

    if not daily_rows:
        return pd.DataFrame()

    out = pd.DataFrame(daily_rows).sort_values("trade_date", ascending=False)
    return out.head(max(1, int(lookback_days))).reset_index(drop=True)


def _auto_tuned_quality_filters(lookback_days: int = 5, mode: str = "Balanced") -> dict[str, Any]:
    """Return tuned quality thresholds from recent performance.

    Tighten on weak outcomes; relax slightly on consistent strong outcomes.
    mode: 'Conservative' | 'Balanced' | 'Aggressive'
    """
    if mode == "Conservative":
        base_score = 65.0
        base_rr = 1.40
        base_edge = 70.0
    elif mode == "Aggressive":
        base_score = 45.0
        base_rr = 0.90
        base_edge = 15.0
    else:  # Balanced
        base_score = 55.0
        base_rr = 1.10
        base_edge = 40.0

    stats = _recent_daily_learning_stats(lookback_days=lookback_days)
    if stats.empty:
        return {
            "min_score": base_score,
            "min_rr": base_rr,
            "min_edge": base_edge,
            "days_used": 0,
            "summary": "No recent closed-trade days available; using base thresholds.",
        }

    avg_net = float(stats["net_pnl"].mean())
    avg_win_rate = float(stats["win_rate"].mean())
    avg_trades = float(stats["closed_trades"].mean())

    min_score = base_score
    min_rr = base_rr
    min_edge = base_edge

    if avg_net < 0:
        min_score += 5.0
        min_rr += 0.10
        min_edge += 20.0
    if avg_net < -500:
        min_score += 5.0
        min_rr += 0.10
        min_edge += 20.0
    if avg_win_rate < 0.40:
        min_score += 5.0
        min_rr += 0.10
    if avg_trades > 6 and avg_net < 0:
        min_edge += 20.0

    if avg_net > 300 and avg_win_rate > 0.55 and avg_trades <= 5:
        min_score -= 3.0
        min_rr -= 0.10
        min_edge -= 10.0

    min_score = float(max(40.0, min(85.0, min_score)))
    min_rr = float(max(0.8, min(2.2, min_rr)))
    min_edge = float(max(10.0, min(300.0, min_edge)))

    return {
        "min_score": round(min_score, 1),
        "min_rr": round(min_rr, 2),
        "min_edge": round(min_edge, 2),
        "days_used": int(len(stats)),
        "summary": (
            f"Based on last {len(stats)} day(s): avg net Rs {avg_net:.2f}, "
            f"avg win-rate {avg_win_rate * 100:.1f}%, avg closed trades/day {avg_trades:.1f}"
        ),
    }


def _intraday_charges(side: str, turnover: float) -> float:
    brokerage = min(turnover * 0.0003, 20.0)
    exchange_txn = turnover * 0.0000325
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    stt = turnover * 0.00025 if side == "SELL" else 0.0
    return brokerage + exchange_txn + sebi + gst + stamp + stt


def _estimate_buy_block(price: float, qty: int) -> float:
    if qty <= 0 or price <= 0:
        return 0.0
    order_value = float(qty) * float(price)
    return order_value * INTRADAY_MARGIN_RATE + _intraday_charges("BUY", order_value)


def _expected_edge_after_costs(entry_price: float, qty: int, plan: dict[str, Any]) -> tuple[float, float]:
    """Return (expected_net_edge_rs, reward_to_risk) for a planned buy trade."""
    if qty <= 0 or entry_price <= 0:
        return 0.0, 0.0

    entry = float(plan.get("entry_price") or entry_price)
    sl = float(plan.get("sl") or 0.0)
    tp = float(plan.get("tp") or 0.0)
    if sl <= 0 or tp <= 0 or entry <= 0:
        return 0.0, 0.0

    reward_per_share = max(0.0, tp - entry)
    risk_per_share = max(0.0, entry - sl)
    gross_reward = reward_per_share * float(qty)

    buy_turnover = entry * float(qty)
    sell_turnover = max(tp, entry) * float(qty)
    est_roundtrip_cost = _intraday_charges(
        "BUY", buy_turnover) + _intraday_charges("SELL", sell_turnover)

    expected_net_edge = gross_reward - est_roundtrip_cost
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0.0
    return float(expected_net_edge), float(rr)


def _passes_buy_quality_gate(
    score: float,
    entry_price: float,
    qty: int,
    plan: dict[str, Any],
    cfg: TradingConfig,
) -> tuple[bool, str]:
    """Gate auto-buys using score, expected edge after costs, and reward/risk."""
    min_score = float(st.session_state.get("auto_min_score", 55.0))
    min_edge = float(st.session_state.get("auto_min_expected_edge_rs", 40.0))
    min_rr = float(st.session_state.get("auto_min_rr", 1.1))

    # Late-session relax: after noon and before entry cutoff, ease score gate slightly.
    session = MarketSession.from_config(cfg)
    if session.is_late_entry_window(session.now()):
        min_score = max(35.0, min_score - 5.0)

    if float(score) < min_score:
        return False, f"score {float(score):.1f} < min {min_score:.1f}"

    exp_edge, rr = _expected_edge_after_costs(
        entry_price=entry_price, qty=qty, plan=plan)
    if rr < min_rr:
        return False, f"R:R {rr:.2f} < min {min_rr:.2f}"
    if exp_edge < min_edge:
        return False, f"expected edge Rs {exp_edge:.2f} < min Rs {min_edge:.2f}"

    return True, ""


def _max_allowed_buy_qty(price: float) -> int:
    if price <= 0:
        return 0
    qty_by_notional = int(MAX_BUY_NOTIONAL_PER_STOCK // float(price))
    return max(0, min(int(MAX_POSITION_QTY_PER_STOCK), int(qty_by_notional)))


def _auto_qty_from_balance(
    price: float,
    live_cash: float,
    slots_remaining: int,
    fallback_qty: int,
    dynamic_enabled: bool,
    utilization_pct: float,
) -> int:
    qty_cap = _max_allowed_buy_qty(price)
    if qty_cap <= 0:
        return 0

    if not dynamic_enabled:
        return max(1, min(int(fallback_qty), int(qty_cap)))
    if price <= 0 or live_cash <= 0:
        return max(1, min(int(fallback_qty), int(qty_cap)))

    safe_util = max(10.0, min(100.0, float(utilization_pct))) / 100.0
    budget_total = live_cash * safe_util
    budget_per_slot = budget_total / max(1, int(slots_remaining))

    per_share_block = max(price * INTRADAY_MARGIN_RATE, 1e-6)
    qty = max(1, min(int(qty_cap), int(budget_per_slot / per_share_block)))

    while qty > 1 and _estimate_buy_block(price, qty) > budget_per_slot:
        qty -= 1
    while qty > 1 and _estimate_buy_block(price, qty) > live_cash:
        qty -= 1

    return max(0, min(int(qty), int(qty_cap)))


def _add_dummy_funds(amount: float) -> None:
    if amount <= 0:
        raise ValueError("Add amount must be greater than zero")
    st.session_state.paper_cash = float(
        st.session_state.paper_cash) + float(amount)
    st.session_state.paper_starting_cash = float(
        st.session_state.paper_starting_cash) + float(amount)
    st.session_state.paper_trade_log.append(
        {
            "timestamp_ist": market_now(DEFAULT_SESSION.timezone).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": "CASH",
            "side": "FUND_ADD",
            "qty": 0,
            "price": 0.0,
            "value": float(amount),
            "charges": 0.0,
            "margin": 0.0,
            "realized_pnl": 0.0,
            "cash_after": float(st.session_state.paper_cash),
            "note": "Manual dummy funds added",
        }
    )
    _save_paper_state()


def _refresh_open_holding_prices(cfg: TradingConfig, data_source: str) -> None:
    symbols = list(st.session_state.paper_holdings.keys())
    if not symbols:
        return

    for sym in symbols:
        try:
            if data_source == "NSE Quote API (non-Yahoo)":
                q = fetch_nse_quote(sym)
                _update_paper_price(sym, float(q.get("last_price") or 0.0))
            else:
                df = fetch_intraday_data(
                    sym,
                    cfg.interval,
                    "1d",
                    tz=cfg.market_timezone,
                    session=MarketSession.from_config(cfg),
                    max_retries=1,
                    backoff_base=1.0,
                )
                if not df.empty:
                    _update_paper_price(sym, float(df.iloc[-1]["close"]))
        except Exception:
            continue


def _auto_exit_before_close(cfg: TradingConfig) -> tuple[list[str], list[str]]:
    if not _in_auto_exit_window(cfg):
        return [], []

    symbols = list(st.session_state.paper_holdings.keys())
    if not symbols:
        return [], []

    exited: list[str] = []
    failed: list[str] = []
    for sym in symbols:
        pos = st.session_state.paper_holdings.get(sym, {})
        qty = int(float(pos.get("qty", 0.0)))
        if qty <= 0:
            continue

        exit_price = float(st.session_state.paper_prices.get(sym, 0.0) or 0.0)
        if exit_price <= 0:
            exit_price = float(pos.get("avg_price", 0.0) or 0.0)
        if exit_price <= 0:
            continue

        try:
            _execute_paper_order(
                symbol=sym,
                side="SELL",
                qty=qty,
                price=exit_price,
                note="Auto square-off: 10 min before market close",
            )
            _update_paper_price(sym, exit_price)
            exited.append(f"{sym} ({qty})")
        except Exception as err:
            failed.append(f"{sym}: {err}")

    return exited, failed


def _update_paper_price(symbol: str, price: float | None) -> None:
    if price is None:
        return
    if price <= 0:
        return
    st.session_state.paper_prices[symbol] = float(price)
    st.session_state.paper_price_updates[symbol] = market_now(
        DEFAULT_SESSION.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
    _save_paper_state()


def _execute_paper_order(symbol: str, side: str, qty: int, price: float, note: str) -> None:
    raise RuntimeError(
        "Legacy dashboard order execution is disabled. Use the authenticated "
        "platform PAPER service so TradingService and RiskEngine remain authoritative.")


def _enforce_existing_position_qty_cap() -> list[str]:
    """Clamp existing open positions to per-stock qty and notional caps, release extra margin to cash."""
    holdings: dict[str, dict[str, float]
                   ] = st.session_state.get("paper_holdings", {})
    if not holdings:
        return []

    adjustments: list[str] = []
    for sym, pos in list(holdings.items()):
        qty = int(float(pos.get("qty", 0.0) or 0.0))
        avg_price = float(pos.get("avg_price", 0.0) or 0.0)
        margin_used = float(pos.get("margin_used", 0.0) or 0.0)

        # Enforce both qty and notional caps
        max_qty_by_qty_cap = MAX_POSITION_QTY_PER_STOCK
        max_qty_by_notional_cap = int(
            MAX_BUY_NOTIONAL_PER_STOCK // avg_price) if avg_price > 0 else 0
        target_qty = min(max_qty_by_qty_cap, max_qty_by_notional_cap)

        if qty <= target_qty:
            continue

        # Apply capping
        reduced_qty = qty - target_qty
        target_margin = target_qty * avg_price * INTRADAY_MARGIN_RATE
        margin_release = max(0.0, margin_used - target_margin)

        holdings[sym]["qty"] = float(target_qty)
        holdings[sym]["margin_used"] = float(target_margin)
        st.session_state.paper_cash = float(
            st.session_state.paper_cash) + float(margin_release)

        # Determine reason for cap
        notional_before = qty * avg_price
        notional_after = target_qty * avg_price
        reason = ""
        if qty > MAX_POSITION_QTY_PER_STOCK:
            reason += f"qty {qty} > limit {MAX_POSITION_QTY_PER_STOCK}; "
        if notional_before > MAX_BUY_NOTIONAL_PER_STOCK:
            reason += f"notional Rs {notional_before:.2f} > limit Rs {MAX_BUY_NOTIONAL_PER_STOCK:.2f}"

        trade_row = {
            "timestamp_ist": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym,
            "side": "ADJUST",
            "qty": int(reduced_qty),
            "price": float(avg_price),
            "value": 0.0,
            "charges": 0.0,
            "margin": float(-margin_release),
            "realized_pnl": 0.0,
            "cash_after": float(st.session_state.paper_cash),
            "note": f"Auto cap applied ({reason.rstrip()}): reduced holding to {target_qty}",
        }
        st.session_state.paper_trade_log.append(trade_row)
        _append_trade_history_row(trade_row)
        adjustments.append(
            f"{sym}: {qty} -> {target_qty} (notional Rs {notional_before:.2f} -> Rs {notional_after:.2f}, released margin Rs {margin_release:.2f})"
        )

    if adjustments:
        _save_paper_state()

    return adjustments


def _portfolio_snapshot() -> tuple[pd.DataFrame, float, float, float]:
    holdings: dict[str, dict[str, float]] = st.session_state.paper_holdings
    prices: dict[str, float] = st.session_state.paper_prices

    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    total_notional = 0.0
    total_margin_used = 0.0
    total_unrealized = 0.0

    for symbol, pos in holdings.items():
        qty = float(pos["qty"])
        avg_price = float(pos["avg_price"])
        ltp = float(prices.get(symbol, avg_price))

        cost_value = qty * avg_price
        market_value = qty * ltp
        margin_used = float(
            pos.get("margin_used", cost_value * INTRADAY_MARGIN_RATE))
        unrealized = market_value - cost_value

        total_cost += cost_value
        total_notional += market_value
        total_margin_used += margin_used
        total_unrealized += unrealized

        rows.append(
            {
                "symbol": symbol,
                "qty": int(qty),
                "avg_price": avg_price,
                "ltp": ltp,
                "cost_value": cost_value,
                "market_value": market_value,
                "margin_used": margin_used,
                "unrealized_pnl": unrealized,
            }
        )

    df = pd.DataFrame(rows)
    cash = float(st.session_state.paper_cash)
    total_equity = cash + total_margin_used + total_unrealized
    return df, cash, total_margin_used, total_equity


def fetch_signals(symbol: str, cfg: TradingConfig) -> tuple[pd.DataFrame | None, float | None, str | None]:
    try:
        cp = _cache_path(symbol, cfg.interval, cfg.period)
        cache_age_sec = (
            pytime.time() - cp.stat().st_mtime) if cp.exists() else None
        df = fetch_intraday_data(
            symbol,
            cfg.interval,
            cfg.period,
            tz=cfg.market_timezone,
            session=MarketSession.from_config(cfg),
        )
        sdf = add_strategy_columns(df, cfg)
        return sdf, cache_age_sec, None
    except Exception as e:
        return None, None, str(e)


def score_signal(last_row: pd.Series, mode: str, allow_short: bool) -> tuple[float, str, str]:
    close = float(last_row["close"])
    open_ = float(last_row["open"])
    vwap = float(last_row["vwap"])
    or_high = float(last_row["or_high"])
    or_low = float(last_row["or_low"])
    vol_spike = bool(last_row["vol_spike"])
    long_signal = bool(last_row["long_signal"])
    short_signal = bool(last_row["short_signal"])

    bias = "Bullish" if close > vwap else "Bearish"
    score = 0.0
    action = "WAIT"

    if mode == "ORB + VWAP":
        score += 45 if vol_spike else 5
        score += 25 if close > vwap else 0
        score += 20 if close > open_ else 0

        dist_to_high_pct = abs(or_high - close) / max(close, 1e-6) * 100
        score += max(0.0, 15 - dist_to_high_pct * 8)

        if long_signal:
            score += 70
            action = "LONG NOW"
        elif allow_short and short_signal:
            score += 70
            action = "SHORT NOW"
        elif close > vwap and close >= 0.997 * or_high:
            action = "WATCH LONG"
        elif allow_short and close < vwap and close <= 1.003 * or_low:
            action = "WATCH SHORT"

    elif mode == "VWAP Trend":
        vwap_gap_pct = abs(close - vwap) / max(vwap, 1e-6) * 100
        score += min(35.0, vwap_gap_pct * 40)
        score += 30 if vol_spike else 8
        score += 20 if close > open_ else 10

        if close > vwap and vol_spike:
            action = "BUY TREND"
            score += 30
        elif allow_short and close < vwap and vol_spike:
            action = "SELL TREND"
            score += 30

    else:  # OR Reversal
        near_low = close <= or_low * 1.002
        near_high = close >= or_high * 0.998
        score += 35 if vol_spike else 8
        score += 20 if near_low or near_high else 5

        if near_low and close > vwap:
            action = "BUY REVERSAL"
            score += 45
        elif allow_short and near_high and close < vwap:
            action = "SELL REVERSAL"
            score += 45

    return round(score, 2), action, bias


def scan_top_stocks(
    cfg: TradingConfig,
    symbols: list[str],
    mode: str,
    top_n: int,
    quick_period: str,
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    errors: list[str] = []

    scan_cfg = TradingConfig(**cfg.__dict__)
    scan_cfg.period = quick_period

    for sym in symbols:
        try:
            df = fetch_intraday_data(
                sym,
                scan_cfg.interval,
                scan_cfg.period,
                tz=scan_cfg.market_timezone,
                session=MarketSession.from_config(scan_cfg),
                max_retries=1,
                backoff_base=1.0,
            )
            sdf = add_strategy_columns(df, scan_cfg)
            latest = sdf.iloc[-1]
            latest_price = float(latest["close"])
            if latest_price <= 0:
                errors.append(f"{sym}: invalid price from data source")
                continue
            score, action, bias = score_signal(
                latest, mode, scan_cfg.allow_short)
            plan = _strategy_plan_from_yahoo(
                last_row=latest,
                cfg=scan_cfg,
                strategy_mode=mode,
            )

            latest_day = sdf[sdf["date"] == sdf["date"].iloc[-1]]
            day_high = float(latest_day["high"].max())
            day_low = float(latest_day["low"].min())
            day_range_pct = ((day_high - day_low) /
                             max(float(latest["close"]), 1e-6)) * 100

            rows.append(
                {
                    "symbol": sym,
                    "price": latest_price,
                    "score": score,
                    "action": action,
                    "bias": bias,
                    "vol_spike": "YES" if bool(latest["vol_spike"]) else "NO",
                    "range_pct": round(day_range_pct, 2),
                    "expected_entry": float(plan.get("entry_price") or 0.0),
                    "trigger_hit": bool(plan.get("trigger_hit")),
                    "trigger_reason": str(plan.get("reason") or ""),
                    "planned_sl": float(plan.get("sl") or 0.0),
                    "planned_tp": float(plan.get("tp") or 0.0),
                    "updated": format_market_timestamp(sdf.index[-1], scan_cfg.market_timezone),
                }
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")

    out = pd.DataFrame(rows)
    if out.empty:
        return out, errors

    out = out.sort_values(by=["score", "range_pct", "price"], ascending=[
                          False, False, False])
    return out, errors


def _auto_trade_engine(
    cfg: TradingConfig,
    data_source: str,
    strategy_mode: str,
    auto_buy_qty: int,
    auto_buy_enabled: bool,
    auto_sell_enabled: bool,
    max_open_positions: int,
    auto_spread_by_balance: bool,
    auto_spread_utilization_pct: float,
    scan_base_top_n: int,
    scan_expanded_top_n: int,
    scan_expansion_review_time: time,
    scan_expansion_min_score: float,
    enable_scan_expansion: bool,
    scan_max_per_sector: int,
    scan_enable_cap_quota: bool,
    scan_large_quota: int,
    scan_mid_quota: int,
    scan_small_quota: int,
    scan_ignore_large_cap: bool,
    scan_min_price: float,
    scan_max_price: float,
) -> list[str]:
    """Run one cycle of automated buy/sell logic. Returns a list of action log strings."""
    actions: list[str] = []
    sold_this_cycle: set[str] = set()  # prevent re-buying a just-exited stock

    # ── AUTO-SELL: exit open positions that hit SL, TP, or square-off ─────────
    if auto_sell_enabled:
        holdings_copy = list(st.session_state.paper_holdings.items())
        for sym, pos in holdings_copy:
            # Re-check after each iteration — a prior sell may have closed this
            if sym not in st.session_state.paper_holdings:
                continue

            qty = int(float(pos.get("qty", 0.0)))
            if qty <= 0:
                continue

            avg_price = float(pos.get("avg_price", 0.0))
            if avg_price <= 0:
                continue

            # Fetch latest price
            ltp = float(st.session_state.paper_prices.get(sym, avg_price))
            if data_source == "NSE Quote API (non-Yahoo)":
                try:
                    q = fetch_nse_quote(sym)
                    ltp = float(q.get("last_price") or ltp)
                    _update_paper_price(sym, ltp)
                except Exception:
                    pass

            if ltp <= 0:
                continue  # no valid price to sell at

            position_sl = avg_price * (1 - cfg.stop_loss_pct)
            position_tp = avg_price * (1 + cfg.take_profit_pct)

            exit_reason: str | None = None
            if ltp >= position_tp:
                exit_reason = f"TP hit @ Rs {ltp:.2f} (target Rs {position_tp:.2f})"
            elif ltp <= position_sl:
                exit_reason = f"SL hit @ Rs {ltp:.2f} (stop Rs {position_sl:.2f})"
            elif _in_square_off_window(cfg):
                exit_reason = f"Square-off time @ Rs {ltp:.2f}"
            elif _in_auto_exit_window(cfg):
                exit_reason = f"Auto square-off before close @ Rs {ltp:.2f}"

            if exit_reason:
                try:
                    _execute_paper_order(
                        symbol=sym,
                        side="SELL",
                        qty=qty,
                        price=ltp,
                        note=f"AutoTrade SELL — {exit_reason}",
                    )
                    sold_this_cycle.add(sym)
                    actions.append(f"SELL {sym}: {exit_reason}")
                except Exception as err:
                    actions.append(f"SELL {sym} FAILED: {err}")

    # ── AUTO-BUY: enter new positions from Top 5 triggered stocks ─────────────
    if auto_buy_enabled and _in_entry_window(cfg):
        n_open = len(st.session_state.paper_holdings)
        buys_today = _today_buy_count()
        if buys_today >= int(cfg.max_trades_per_day):
            actions.append(
                f"AUTO-BUY paused: max {int(cfg.max_trades_per_day)} trades reached for today"
            )
            return actions
        if n_open >= max_open_positions:
            actions.append(
                f"AUTO-BUY paused: max {max_open_positions} concurrent positions reached"
            )
            return actions

        seen_symbols: set[str] = set()

        for order in sorted(_pending_buy_orders(), key=lambda item: str(item.get("created_at", ""))):
            if n_open >= max_open_positions:
                break

            sym = _normalize_symbol(str(order.get("symbol", "")))
            if not sym or sym in seen_symbols:
                continue
            seen_symbols.add(sym)

            if sym in st.session_state.paper_holdings:
                continue
            if sym in sold_this_cycle:
                actions.append(
                    f"BUY {sym} SKIPPED: just exited this cycle (SL/TP), cooling off")
                continue

            target_price = float(order.get("target_price", 0.0) or 0.0)
            qty = int(order.get("qty", auto_buy_qty) or auto_buy_qty)
            order_strategy = str(order.get("strategy") or strategy_mode)
            try:
                price, _ = _fetch_live_plan(
                    sym,
                    cfg,
                    order_strategy,
                    data_source,
                    fallback_price=target_price,
                )
            except Exception as err:
                actions.append(f"PENDING BUY {sym} FAILED TO EVALUATE: {err}")
                continue

            if price <= 0 or target_price <= 0 or price > target_price:
                continue

            qty_cap = _max_allowed_buy_qty(price)
            if qty_cap <= 0:
                actions.append(
                    f"PENDING BUY {sym} SKIPPED: price too high for Rs {MAX_BUY_NOTIONAL_PER_STOCK:.0f} cap"
                )
                continue
            if qty > qty_cap:
                actions.append(
                    f"PENDING BUY {sym}: qty limited from {qty} to {qty_cap} by risk caps"
                )
                qty = qty_cap

            est_value = float(qty) * price
            est_charges = _intraday_charges("BUY", est_value)
            est_block = est_value * INTRADAY_MARGIN_RATE + est_charges
            live_cash = float(st.session_state.paper_cash)
            if live_cash < est_block:
                actions.append(
                    f"PENDING BUY {sym} SKIPPED: insufficient cash (need Rs {est_block:.2f}, have Rs {live_cash:.2f})"
                )
                continue

            try:
                _execute_paper_order(
                    symbol=sym,
                    side="BUY",
                    qty=qty,
                    price=price,
                    note=f"Pending BUY fill ({order_strategy}) @<= Rs {target_price:.2f}",
                )
                _update_paper_price(sym, price)
                _remove_pending_buy_order(sym)
                actions.append(
                    f"PENDING BUY FILLED {sym} {qty}qty @ Rs {price:.2f} (target Rs {target_price:.2f})"
                )
                n_open += 1
                buys_today += 1
                if buys_today >= int(cfg.max_trades_per_day):
                    actions.append(
                        f"AUTO-BUY paused: max {int(cfg.max_trades_per_day)} trades reached for today"
                    )
                    break
            except Exception as err:
                actions.append(f"PENDING BUY {sym} FAILED: {err}")

        for item in _armed_symbols():
            if n_open >= max_open_positions:
                break

            sym = _normalize_symbol(str(item.get("symbol", "")))
            if not sym or sym in seen_symbols:
                continue
            seen_symbols.add(sym)

            if sym in st.session_state.paper_holdings:
                continue
            if sym in sold_this_cycle:
                actions.append(
                    f"BUY {sym} SKIPPED: just exited this cycle (SL/TP), cooling off")
                continue

            armed_strategy = str(item.get("strategy") or strategy_mode)
            try:
                price, live_plan = _fetch_live_plan(
                    sym,
                    cfg,
                    armed_strategy,
                    data_source,
                )
            except Exception as err:
                actions.append(f"ARMED WATCH {sym} FAILED TO EVALUATE: {err}")
                continue

            trigger = bool(live_plan.get("trigger_hit"))
            if not trigger or price <= 0:
                continue

            slots_remaining = max(1, max_open_positions - n_open)
            buy_qty = _auto_qty_from_balance(
                price=price,
                live_cash=float(st.session_state.paper_cash),
                slots_remaining=slots_remaining,
                fallback_qty=auto_buy_qty,
                dynamic_enabled=auto_spread_by_balance,
                utilization_pct=auto_spread_utilization_pct,
            )
            if buy_qty <= 0:
                actions.append(
                    f"ARMED BUY {sym} SKIPPED: risk caps allow 0 qty at current price"
                )
                continue

            est_value = float(buy_qty) * price
            est_charges = _intraday_charges("BUY", est_value)
            est_block = est_value * INTRADAY_MARGIN_RATE + est_charges
            live_cash = float(st.session_state.paper_cash)
            if live_cash < est_block:
                actions.append(
                    f"ARMED BUY {sym} SKIPPED: insufficient cash (need Rs {est_block:.2f}, have Rs {live_cash:.2f})"
                )
                continue

            try:
                _execute_paper_order(
                    symbol=sym,
                    side="BUY",
                    qty=buy_qty,
                    price=price,
                    note=f"Armed AutoTrade BUY ({armed_strategy}) — trigger hit",
                )
                _update_paper_price(sym, price)
                actions.append(
                    f"ARMED BUY {sym} {buy_qty}qty @ Rs {price:.2f}"
                )
                n_open += 1
                buys_today += 1
                if buys_today >= int(cfg.max_trades_per_day):
                    actions.append(
                        f"AUTO-BUY paused: max {int(cfg.max_trades_per_day)} trades reached for today"
                    )
                    break
            except Exception as err:
                actions.append(f"ARMED BUY {sym} FAILED: {err}")

        target_scan_n, expansion_active = _scan_target_count(
            cfg=cfg,
            base_top_n=scan_base_top_n,
            expanded_top_n=scan_expanded_top_n,
            expansion_review_time=scan_expansion_review_time,
            enable_expansion=enable_scan_expansion,
        )

        # Reuse session-state scan results if available; avoid re-scanning every cycle
        ranked_df: pd.DataFrame = st.session_state.get(
            "top5_df", pd.DataFrame())
        if ranked_df.empty:
            try:
                scan_symbols = WATCHLIST
                if data_source == "NSE Quote API (non-Yahoo)":
                    ranked_df, _ = scan_top_stocks_nse(
                        symbols=scan_symbols,
                        mode=strategy_mode,
                        top_n=target_scan_n,
                        allow_short=cfg.allow_short,
                    )
                else:
                    ranked_df, _ = scan_top_stocks(
                        cfg=cfg,
                        symbols=scan_symbols,
                        mode=strategy_mode,
                        top_n=target_scan_n,
                        quick_period="5d",
                    )
            except Exception:
                return actions

        ranked_df = _apply_price_band_filter(
            ranked_df=ranked_df,
            min_price=scan_min_price,
            max_price=scan_max_price,
        )
        ranked_df = _apply_cap_focus_filter(
            ranked_df=ranked_df,
            ignore_large_cap=scan_ignore_large_cap,
        )
        ranked_df = _apply_scan_expansion_filter(
            ranked_df=ranked_df,
            base_top_n=scan_base_top_n,
            expanded_top_n=scan_expanded_top_n,
            extra_min_score=scan_expansion_min_score,
            expansion_active=expansion_active,
        )
        ranked_df = _apply_sector_diversity_cap(
            ranked_df=ranked_df,
            max_per_sector=scan_max_per_sector,
            top_n=target_scan_n,
        )
        ranked_df = _apply_market_cap_quota(
            ranked_df=ranked_df,
            top_n=target_scan_n,
            enable_quota=scan_enable_cap_quota,
            large_quota=scan_large_quota,
            mid_quota=scan_mid_quota,
            small_quota=scan_small_quota,
        )

        if ranked_df.empty:
            return actions

        for _, row in ranked_df.iterrows():
            if n_open >= max_open_positions:
                break

            sym = str(row["symbol"])
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)
            price = float(row["price"])
            if price <= 0:
                actions.append(f"BUY {sym} SKIPPED: invalid live/scan price")
                continue
            candidate_score = float(row.get("score", 0.0) or 0.0)
            trigger = bool(row.get("trigger_hit", False))
            live_plan = {
                "entry_price": float(row.get("expected_entry", price) or price),
                "trigger_hit": trigger,
                "sl": float(row.get("planned_sl", 0.0) or 0.0),
                "tp": float(row.get("planned_tp", 0.0) or 0.0),
            }

            # Skip: already holding this stock
            if sym in st.session_state.paper_holdings:
                continue

            # Skip: just sold this stock this cycle (e.g. SL hit)
            if sym in sold_this_cycle:
                actions.append(
                    f"BUY {sym} SKIPPED: just exited this cycle (SL/TP), cooling off")
                continue

            # Refresh trigger with live quote (NSE mode)
            if data_source == "NSE Quote API (non-Yahoo)":
                try:
                    live_q = fetch_nse_quote(sym)
                    live_price = float(live_q.get("last_price") or 0.0)
                    if live_price > 0:
                        price = live_price
                    live_plan = _strategy_plan_from_nse_quote(
                        quote=live_q, cfg=cfg, strategy_mode=strategy_mode
                    )
                    trigger = bool(live_plan.get("trigger_hit"))
                except Exception:
                    pass  # use scanned values as fallback

            if not trigger or price <= 0:
                continue

            slots_remaining = max(1, max_open_positions - n_open)
            buy_qty = _auto_qty_from_balance(
                price=price,
                live_cash=float(st.session_state.paper_cash),
                slots_remaining=slots_remaining,
                fallback_qty=auto_buy_qty,
                dynamic_enabled=auto_spread_by_balance,
                utilization_pct=auto_spread_utilization_pct,
            )
            if buy_qty <= 0:
                actions.append(
                    f"BUY {sym} SKIPPED: risk caps allow 0 qty at current price"
                )
                continue

            passes_gate, gate_reason = _passes_buy_quality_gate(
                score=candidate_score,
                entry_price=price,
                qty=buy_qty,
                plan=live_plan,
                cfg=cfg,
            )
            if not passes_gate:
                actions.append(
                    f"BUY {sym} SKIPPED: quality gate ({gate_reason})")
                continue

            est_value = float(buy_qty) * price
            est_charges = _intraday_charges("BUY", est_value)
            est_block = est_value * INTRADAY_MARGIN_RATE + est_charges
            live_cash = float(st.session_state.paper_cash)

            if live_cash < est_block:
                actions.append(
                    f"BUY {sym} SKIPPED: insufficient cash (need Rs {est_block:.2f}, have Rs {live_cash:.2f})"
                )
                continue

            try:
                _execute_paper_order(
                    symbol=sym,
                    side="BUY",
                    qty=buy_qty,
                    price=price,
                    note=f"AutoTrade BUY ({strategy_mode}) — trigger hit",
                )
                _update_paper_price(sym, price)
                actions.append(
                    f"BUY {sym} {buy_qty}qty @ Rs {price:.2f}"
                )
                n_open += 1
                buys_today += 1
                if buys_today >= int(cfg.max_trades_per_day):
                    actions.append(
                        f"AUTO-BUY paused: max {int(cfg.max_trades_per_day)} trades reached for today"
                    )
                    break
            except Exception as err:
                actions.append(f"BUY {sym} FAILED: {err}")

    return actions


def _to_nse_symbol(symbol: str) -> str:
    return symbol.split(".")[0].upper()


@st.cache_data(ttl=8, show_spinner=False)
def _fetch_nse_quote_payload(nse_symbol: str) -> dict[str, Any]:
    if nsefetch is None:
        raise ValueError(
            "nsepython is not installed. Run: pip install nsepython")
    return nsefetch(
        f"https://www.nseindia.com/api/quote-equity?symbol={nse_symbol}"
    )


def fetch_nse_quote(symbol: str) -> dict:
    nse_symbol = _to_nse_symbol(symbol)
    data = _fetch_nse_quote_payload(nse_symbol)
    p = data.get("priceInfo", {})

    last_price = float(p.get("lastPrice") or 0)
    open_price = float(p.get("open") or 0)
    vwap = float(p.get("vwap") or 0)
    pchange = float(p.get("pChange") or 0)

    ihl = p.get("intraDayHighLow", {}) or {}
    day_low = float(ihl.get("min") or 0)
    day_high = float(ihl.get("max") or 0)
    day_range_pct = ((day_high - day_low) / max(last_price,
                     1e-6)) * 100 if last_price > 0 else 0.0

    return {
        "symbol": symbol,
        "last_price": last_price,
        "open": open_price,
        "vwap": vwap,
        "pchange": pchange,
        "day_low": day_low,
        "day_high": day_high,
        "day_range_pct": day_range_pct,
    }


def scan_top_stocks_nse(symbols: list[str], mode: str, top_n: int, allow_short: bool) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    errors: list[str] = []

    for sym in symbols:
        try:
            q = fetch_nse_quote(sym)
            price = q["last_price"]
            if float(price) <= 0:
                errors.append(f"{sym}: invalid quote (price<=0)")
                continue
            vwap = q["vwap"]
            pchange = q["pchange"]
            day_range_pct = q["day_range_pct"]

            tmp_cfg = TradingConfig(
                symbol=sym,
                allow_short=allow_short,
            )
            plan = _strategy_plan_from_nse_quote(
                quote=q,
                cfg=tmp_cfg,
                strategy_mode=mode,
            )

            score = 0.0
            action = "WAIT"

            if mode == "VWAP Trend":
                score += min(40.0, abs(price - vwap) /
                             max(vwap, 1e-6) * 400) if vwap > 0 else 0
                score += min(35.0, abs(pchange) * 5)
                score += min(25.0, day_range_pct * 2)
                if vwap > 0 and price > vwap and pchange > 0.3:
                    action = "BUY TREND"
                elif allow_short and vwap > 0 and price < vwap and pchange < -0.3:
                    action = "SELL TREND"
            elif mode == "OR Reversal":
                score += min(35.0, day_range_pct * 3)
                score += min(35.0, abs(pchange) * 4)
                score += 20 if vwap > 0 else 0
                if pchange < -0.8 and (vwap == 0 or price > vwap):
                    action = "BUY REVERSAL"
                elif allow_short and pchange > 0.8 and (vwap == 0 or price < vwap):
                    action = "SELL REVERSAL"
            else:
                score += min(40.0, day_range_pct * 2.5)
                score += min(30.0, abs(pchange) * 4)
                score += 30 if (vwap > 0 and price > vwap) else 10
                if vwap > 0 and price > vwap and pchange > 0.4:
                    action = "WATCH LONG"
                elif allow_short and vwap > 0 and price < vwap and pchange < -0.4:
                    action = "WATCH SHORT"

            rows.append(
                {
                    "symbol": sym,
                    "price": price,
                    "score": round(score, 2),
                    "action": action,
                    "bias": "Bullish" if (vwap > 0 and price > vwap) else "Bearish",
                    "chg_pct": round(pchange, 2),
                    "range_pct": round(day_range_pct, 2),
                    "expected_entry": float(plan.get("entry_price") or 0.0),
                    "trigger_hit": bool(plan.get("trigger_hit")),
                    "trigger_reason": str(plan.get("reason") or ""),
                    "planned_sl": float(plan.get("sl") or 0.0),
                    "planned_tp": float(plan.get("tp") or 0.0),
                    "updated": market_now(tmp_cfg.market_timezone).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")

    out = pd.DataFrame(rows)
    if out.empty:
        return out, errors

    out = out.sort_values(by=["score", "range_pct", "chg_pct"], ascending=[
                          False, False, False])
    return out, errors


st.set_page_config(page_title="NSE/BSE Intraday Dashboard",
                   page_icon="📈", layout="wide")
st.markdown("### 📈 NSE/BSE Intraday Paper-Testing Dashboard")

boot_cfg = load_config("config.json")
now_market = market_now(boot_cfg.market_timezone)
boot_session = MarketSession.from_config(boot_cfg)
phase, advice, phase_color = market_phase(now_market, boot_session)

c1, c2, c3 = st.columns([1.2, 1.4, 2.4])
with c1:
    st.caption(f"Market Time ({boot_cfg.market_timezone})")
    st.markdown(f"**{now_market.strftime('%I:%M:%S %p')}**")
with c2:
    st.caption(now_market.strftime("%A, %d %B %Y"))
    st.caption("System locale ignored")
with c3:
    st.markdown(
        f"<div style='background:{phase_color};padding:8px 10px;border-radius:6px;color:white;font-size:12px;'>"
        f"<b>{phase}</b> | {advice}</div>",
        unsafe_allow_html=True,
    )

st.divider()

with st.sidebar:
    st.header("Controls")
    cfg = load_config("config.json")

    st.subheader("Budget Setup")
    starting_capital_input = float(st.number_input(
        "Total capital (Rs)",
        min_value=1000.0,
        value=float(cfg.starting_capital),
        step=1000.0,
        format="%.2f",
    ))
    risk_per_trade_input = float(st.slider(
        "Risk per trade (%)",
        min_value=0.1,
        max_value=5.0,
        value=float(cfg.risk_per_trade_pct * 100.0),
        step=0.1,
    ))
    max_trades_per_day_input = int(st.number_input(
        "Max trades per day",
        min_value=1,
        max_value=20,
        value=int(cfg.max_trades_per_day),
        step=1,
    ))
    st.caption(
        "Reset portfolio if you want a new starting capital applied immediately."
    )

    data_source = st.selectbox(
        "Data source",
        ["NSE Quote API (non-Yahoo)", "Yahoo (OHLC bars)"],
        index=0,
    )

    strategy_mode = st.selectbox(
        "Paper strategy mode",
        ["ORB + VWAP", "VWAP Trend", "OR Reversal"],
        index=0,
    )

    symbol = st.selectbox(
        "Primary Symbol",
        options=WATCHLIST,
        index=WATCHLIST.index(cfg.symbol) if cfg.symbol in WATCHLIST else 0,
    )

    custom_symbol = st.text_input("Custom Symbol (example: ONGC.NS)")
    if custom_symbol.strip():
        symbol = custom_symbol.strip().upper()

    # Keep risk and detail settings from config to reduce page clutter.
    opening_range_minutes = cfg.opening_range_minutes
    stop_loss_pct = cfg.stop_loss_pct
    take_profit_pct = cfg.take_profit_pct
    volume_spike = cfg.volume_spike_threshold
    allow_short = cfg.allow_short
    period = cfg.period if cfg.period in [
        "5d", "10d", "15d", "30d", "60d"] else "30d"
    bars_to_show = 10
    st.caption(
        f"Using config defaults: SL {stop_loss_pct * 100:.2f}%, TP {take_profit_pct * 100:.2f}%, period {period}, short {'on' if allow_short else 'off'}."
    )

    st.subheader("Top-5 Scanner")
    scan_universe_size = st.slider(
        "Symbols to scan", 5, len(WATCHLIST), min(20, len(WATCHLIST)))
    scan_period = st.selectbox(
        "Scanner period", ["5d", "10d", "15d", "30d"], index=0)
    simplified_scanner = st.checkbox(
        "Simplified scanner",
        value=True,
        help="Use a simpler setup focused on low+mid price stocks.",
    )
    scan_min_price = float(st.number_input(
        "Minimum stock price (Rs)",
        min_value=0.0,
        max_value=50000.0,
        value=0.0,
        step=50.0,
        format="%.2f",
    ))
    scan_max_price = float(st.number_input(
        "Maximum stock price (Rs)",
        min_value=1.0,
        max_value=50000.0,
        value=1800.0,
        step=50.0,
        format="%.2f",
    ))

    if simplified_scanner:
        st.caption(
            "Simple mode active: low+mid price focus (up to selected max price).")
        enable_top8_expansion = False
        top8_review_time = time(10, 30)
        top8_extra_min_score = 0.0
        scan_max_per_sector = 2
        scan_enable_cap_quota = False
        scan_ignore_large_cap = True
        scan_large_quota = 0
        scan_mid_quota = 3
        scan_small_quota = 2
    else:
        with st.expander("Advanced scanner filters", expanded=False):
            enable_top8_expansion = st.checkbox(
                "Expand Top 5 to Top 8 later",
                value=True,
                help="After the review time, expand to 8 candidates only if no buy happened yet today.",
            )
            top8_review_time = _parse_time(
                st.text_input("Expand after time", value="10:30",
                              key="top8_review_time"),
                time(10, 30),
            )
            top8_extra_min_score = float(st.number_input(
                "Min score for ranks 6-8",
                min_value=0.0,
                max_value=100.0,
                value=55.0,
                step=1.0,
                format="%.1f",
                key="top8_extra_min_score",
            ))
            scan_max_per_sector = int(st.number_input(
                "Max symbols per sector in Top list",
                min_value=1,
                max_value=5,
                value=1,
                step=1,
                help="Limits sector concentration in Top candidates (example: avoid 4 banks in Top 5).",
                key="scan_max_per_sector",
            ))
            scan_enable_cap_quota = st.checkbox(
                "Diversify across market-cap buckets",
                value=True,
                help="Spread Top candidates across Large/Mid/Small-cap buckets with backfill when needed.",
                key="scan_enable_cap_quota",
            )
            scan_ignore_large_cap = st.checkbox(
                "Ignore Large-cap (focus Mid/Small)",
                value=True,
                help="Exclude large-cap symbols from scan and auto-buy candidate selection.",
                key="scan_ignore_large_cap",
            )
            cap1, cap2, cap3 = st.columns(3)
            with cap1:
                scan_large_quota = int(st.number_input(
                    "Large quota",
                    min_value=0,
                    max_value=8,
                    value=2,
                    step=1,
                    key="scan_large_quota",
                    disabled=not bool(scan_enable_cap_quota),
                ))
            with cap2:
                scan_mid_quota = int(st.number_input(
                    "Mid quota",
                    min_value=0,
                    max_value=8,
                    value=2,
                    step=1,
                    key="scan_mid_quota",
                    disabled=not bool(scan_enable_cap_quota),
                ))
            with cap3:
                scan_small_quota = int(st.number_input(
                    "Small quota",
                    min_value=0,
                    max_value=8,
                    value=1,
                    step=1,
                    key="scan_small_quota",
                    disabled=not bool(scan_enable_cap_quota),
                ))
    scan_button = st.button("Scan Top 5", use_container_width=True)
    refresh = st.button("Refresh Selected Symbol", use_container_width=True)
    reset_paper = st.button("Reset Dummy Portfolio", use_container_width=True)

    st.subheader("Live Refresh")
    auto_refresh_enabled = st.checkbox("Auto refresh (NSE)", value=True)
    auto_refresh_seconds = int(
        st.number_input(
            "Refresh interval seconds",
            min_value=5,
            max_value=300,
            value=30,
            step=5,
        )
    )
    fast_mode = st.checkbox(
        "Fast mode (lighter dashboard)",
        value=True,
        help="Skips non-essential heavy calculations and extra quote refreshes to improve load time.",
    )
    compact_ui = st.checkbox(
        "Compact UI (essentials only)",
        value=True,
        help="Hide heavy non-essential panels by default for faster interaction.",
    )
    refresh_holdings_prices = st.button(
        "Refresh Bought Stock Prices", use_container_width=True)

    st.subheader("🤖 Automated Trading")
    auto_trade_enabled = st.checkbox(
        "Enable Fully Automated Trading",
        value=True,
        help="Automatically buys on trigger and sells on SL/TP every refresh cycle.",
    )
    auto_buy_only = st.checkbox("Auto-Buy only (manual sell)", value=False)
    auto_sell_only = st.checkbox("Auto-Sell only (manual buy)", value=False)
    auto_trade_qty = int(
        st.number_input("Auto-trade qty per stock",
                        min_value=1, value=1, step=1)
    )
    auto_spread_by_balance = st.checkbox(
        "Auto-size qty from balance",
        value=True,
        help="Automatically calculate quantity per buy by splitting available cash across remaining position slots.",
    )
    auto_spread_utilization_pct = float(st.slider(
        "Balance utilization % for auto-size",
        min_value=10,
        max_value=100,
        value=85,
        step=5,
    ))
    auto_max_positions = int(
        st.number_input("Max concurrent positions", min_value=1,
                        max_value=10, value=3, step=1)
    )
    saved_ui_prefs = _load_saved_ui_preferences()
    if "auto_tune_quality" not in st.session_state:
        st.session_state["auto_tune_quality"] = bool(
            saved_ui_prefs.get("auto_tune_quality", True)
        )
    if "tune_mode" not in st.session_state:
        loaded_mode = str(saved_ui_prefs.get("tune_mode", "Balanced"))
        st.session_state["tune_mode"] = (
            loaded_mode if loaded_mode in [
                "Conservative", "Balanced", "Aggressive"] else "Balanced"
        )

    auto_tune_quality = st.checkbox(
        "Auto-tune quality filters (last 5 days)",
        help="Automatically adjusts score/R:R/expected-edge thresholds from recent results.",
        key="auto_tune_quality",
    )
    tune_mode = st.radio(
        "Tuning mode",
        options=["Conservative", "Balanced", "Aggressive"],
        horizontal=True,
        help="Conservative: tighter thresholds (fewer, safer trades). Balanced: data-adaptive defaults. Aggressive: lower bar (more trades, higher risk).",
        key="tune_mode",
        disabled=not bool(auto_tune_quality),
    )

    if "auto_min_score" not in st.session_state:
        st.session_state["auto_min_score"] = 55.0
    if "auto_min_rr" not in st.session_state:
        st.session_state["auto_min_rr"] = 1.1
    if "auto_min_expected_edge_rs" not in st.session_state:
        st.session_state["auto_min_expected_edge_rs"] = 40.0

    tuned = _auto_tuned_quality_filters(lookback_days=5, mode=tune_mode)
    if auto_tune_quality:
        st.session_state["auto_min_score"] = float(tuned["min_score"])
        st.session_state["auto_min_rr"] = float(tuned["min_rr"])
        st.session_state["auto_min_expected_edge_rs"] = float(
            tuned["min_edge"])

    st.markdown("**Auto-buy quality filters**")
    auto_min_score = float(st.slider(
        "Minimum candidate score",
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        key="auto_min_score",
        disabled=bool(auto_tune_quality),
    ))
    auto_min_rr = float(st.slider(
        "Minimum reward:risk",
        min_value=0.5,
        max_value=3.0,
        step=0.1,
        key="auto_min_rr",
        disabled=bool(auto_tune_quality),
    ))
    auto_min_expected_edge_rs = float(st.number_input(
        "Minimum expected edge after costs (Rs)",
        min_value=0.0,
        max_value=5000.0,
        step=10.0,
        format="%.2f",
        key="auto_min_expected_edge_rs",
        disabled=bool(auto_tune_quality),
    ))
    if auto_tune_quality:
        st.caption(
            f"Self-tune active [{tune_mode}]. {tuned['summary']}"
        )
    if auto_trade_enabled:
        st.caption(
            f"Engine active — Auto-{'Buy+Sell' if not auto_buy_only and not auto_sell_only else ('Buy' if auto_buy_only else 'Sell')} | "
            f"Qty mode: {'Balance spread' if auto_spread_by_balance else f'Fixed ({auto_trade_qty})'} | "
            f"Max positions: {auto_max_positions}"
        )
        st.caption(
            f"Risk caps: max {MAX_POSITION_QTY_PER_STOCK} qty/stock and max Rs {MAX_BUY_NOTIONAL_PER_STOCK:.0f} buy value/stock"
        )
        st.caption(
            f"Quality gates: score >= {auto_min_score:.1f}, R:R >= {auto_min_rr:.2f}, expected edge >= Rs {auto_min_expected_edge_rs:.2f}"
        )
        st.warning("Automated trading is running. Monitor positions carefully.")

    st.subheader("Dummy Funds")
    add_funds_amount = st.number_input(
        "Add dummy money",
        min_value=1000.0,
        value=500000.0,
        step=1000.0,
        format="%.2f",
    )
    add_funds_btn = st.button("Add Funds", use_container_width=True)

cfg.symbol = symbol
cfg.starting_capital = starting_capital_input
cfg.risk_per_trade_pct = risk_per_trade_input / 100.0
cfg.max_trades_per_day = max_trades_per_day_input
cfg.opening_range_minutes = opening_range_minutes
cfg.stop_loss_pct = stop_loss_pct
cfg.take_profit_pct = take_profit_pct
cfg.volume_spike_threshold = volume_spike
cfg.allow_short = allow_short
cfg.period = period

if auto_refresh_enabled and data_source == "NSE Quote API (non-Yahoo)":
    st.caption(
        f"Auto refresh active: every {auto_refresh_seconds}s (NSE mode)")

_init_paper_state(cfg.starting_capital)
_ensure_auto_trade_state()
_init_strategy_tracking()

# Persist UI preference changes (e.g., tune mode) so full browser refresh keeps last choice.
pref_auto_tune = bool(st.session_state.get("auto_tune_quality", True))
pref_tune_mode = str(st.session_state.get("tune_mode", "Balanced"))
if st.session_state.get("_persisted_auto_tune_quality") != pref_auto_tune or st.session_state.get("_persisted_tune_mode") != pref_tune_mode:
    st.session_state["_persisted_auto_tune_quality"] = pref_auto_tune
    st.session_state["_persisted_tune_mode"] = pref_tune_mode
    _save_paper_state()

cap_adjustments = _enforce_existing_position_qty_cap()
if cap_adjustments:
    st.warning("Position quantity cap applied: " + " | ".join(cap_adjustments))

if reset_paper:
    _reset_paper_state(cfg.starting_capital)
    st.success("Dummy portfolio reset completed")

if add_funds_btn:
    try:
        _add_dummy_funds(float(add_funds_amount))
        st.success(f"Added Rs {float(add_funds_amount):.2f} to dummy cash")
    except Exception as err:
        st.error(f"Add funds failed: {err}")

if refresh_holdings_prices:
    _refresh_open_holding_prices(cfg, data_source)
    st.success("Bought stock prices refreshed")

if refresh:
    if data_source == "Yahoo (OHLC bars)":
        cp = _cache_path(symbol, cfg.interval, cfg.period)
        cp.unlink(missing_ok=True)
    st.rerun()

if not fast_mode and not compact_ui:
    _refresh_open_holding_prices(cfg, data_source)
auto_exited_symbols, auto_exit_failures = _auto_exit_before_close(cfg)
if auto_exited_symbols:
    st.warning(
        "Auto-exit executed (10 min before close): " +
        ", ".join(auto_exited_symbols)
    )
if auto_exit_failures:
    st.error(
        "Auto-exit attempted but failed for: " + "; ".join(auto_exit_failures)
    )

# ── Fully Automated Trading Engine ────────────────────────────────────────────
_do_auto_buy = auto_trade_enabled and not auto_sell_only
_do_auto_sell = auto_trade_enabled and not auto_buy_only
if auto_trade_enabled:
    engine_actions = _auto_trade_engine(
        cfg=cfg,
        data_source=data_source,
        strategy_mode=strategy_mode,
        auto_buy_qty=auto_trade_qty,
        auto_buy_enabled=_do_auto_buy,
        auto_sell_enabled=_do_auto_sell,
        max_open_positions=auto_max_positions,
        auto_spread_by_balance=auto_spread_by_balance,
        auto_spread_utilization_pct=auto_spread_utilization_pct,
        scan_base_top_n=5,
        scan_expanded_top_n=8,
        scan_expansion_review_time=top8_review_time,
        scan_expansion_min_score=top8_extra_min_score,
        enable_scan_expansion=enable_top8_expansion,
        scan_max_per_sector=scan_max_per_sector,
        scan_enable_cap_quota=scan_enable_cap_quota,
        scan_large_quota=scan_large_quota,
        scan_mid_quota=scan_mid_quota,
        scan_small_quota=scan_small_quota,
        scan_ignore_large_cap=scan_ignore_large_cap,
        scan_min_price=scan_min_price,
        scan_max_price=scan_max_price,
    )
    did_auto_buy = any(
        act.startswith("BUY ") or act.startswith("ARMED BUY ")
        for act in engine_actions
    )
    if did_auto_buy:
        _refresh_open_holding_prices(cfg, data_source)
    if engine_actions:
        with st.expander("🤖 Auto-Trade Engine — Actions This Cycle", expanded=True):
            for act in engine_actions:
                color = "#1b8a3f" if act.startswith("BUY ") and "FAILED" not in act and "SKIPPED" not in act else (
                    "#c62828" if "FAILED" in act else "#fd7e14"
                )
                st.markdown(
                    f"<span style='color:{color};font-weight:600;'>▶ {act}</span>",
                    unsafe_allow_html=True,
                )
    else:
        st.caption("🤖 Auto-trade engine: no actions this cycle.")

top_holdings_df, top_cash, top_margin_used, top_equity = _portfolio_snapshot()
top_invested = float(top_holdings_df["cost_value"].sum(
)) if not top_holdings_df.empty else 0.0
top_unrealized = float(
    top_holdings_df["unrealized_pnl"].sum()) if not top_holdings_df.empty else 0.0
top_realized = float(st.session_state.paper_realized_pnl)
top_charges = float(st.session_state.paper_total_charges)

unreal_color = "#1b8a3f" if top_unrealized >= 0 else "#c62828"
realized_color = "#1b8a3f" if top_realized >= 0 else "#c62828"

st.markdown(
    f"""
<div style=\"font-size:12px;line-height:1.5;border:1px solid #ddd;border-radius:8px;padding:8px 10px;background:#fafafa;\">
  <b>dummy portfolio (paper trading)</b> |
  cash: Rs {top_cash:.2f} |
  margin used: Rs {top_margin_used:.2f} |
  total equity: Rs {top_equity:.2f} |
  invested cost: Rs {top_invested:.2f} |
  unrealized pnl: <span style=\"color:{unreal_color};font-weight:600;\">Rs {top_unrealized:.2f}</span> |
  realized pnl: <span style=\"color:{realized_color};font-weight:600;\">Rs {top_realized:.2f}</span> |
  total charges: Rs {top_charges:.2f}
</div>
""",
    unsafe_allow_html=True,
)

if not top_holdings_df.empty:
    holding_chunks: list[str] = []
    for _, hrow in top_holdings_df.sort_values(by="symbol").iterrows():
        hs = str(hrow["symbol"])
        hq = int(float(hrow.get("qty", 0.0)))
        havg = float(hrow.get("avg_price", 0.0))
        hltp = float(hrow.get("ltp", 0.0))
        hpnl = float(hrow.get("unrealized_pnl", 0.0))
        hpnl_color = "#1b8a3f" if hpnl >= 0 else "#c62828"
        holding_chunks.append(
            f"{hs} qty {hq} | avg Rs {havg:.2f} | ltp Rs {hltp:.2f} | "
            f"pnl <span style='color:{hpnl_color};font-weight:600;'>Rs {hpnl:.2f}</span>"
        )

    st.markdown(
        "<div style=\"font-size:12px;line-height:1.5;border:1px solid #e5e7eb;border-radius:8px;padding:8px 10px;"
        "background:#ffffff;margin-top:6px;\">"
        f"<b>Current Holdings</b>: {' | '.join(holding_chunks)}"
        "</div>",
        unsafe_allow_html=True,
    )
else:
    st.caption("Current Holdings: None")

st.divider()
st.subheader("Persistent Orders & Watchlist")
st.caption(
    "Pending buys stay active until filled or canceled. Armed symbols remain watched even if they drop out of Top 5."
)

default_pending_symbol_index = WATCHLIST.index(
    symbol) if symbol in WATCHLIST else 0
default_pending_price = float(
    st.session_state.paper_prices.get(symbol, 0.0) or 1.0)

pm1, pm2, pm3 = st.columns([1.5, 1, 1])
pending_order_symbol = pm1.selectbox(
    "Pending buy symbol",
    options=WATCHLIST,
    index=default_pending_symbol_index,
    key="pending_order_symbol",
)
pending_order_price = pm2.number_input(
    "Buy at or below",
    min_value=0.01,
    value=default_pending_price,
    step=0.05,
    format="%.2f",
    key="pending_order_price",
)
pending_order_qty = int(pm3.number_input(
    "Pending qty",
    min_value=1,
    value=auto_trade_qty,
    step=1,
    key="pending_order_qty",
))

pm4, pm5 = st.columns(2)
if pm4.button("Add Pending Buy", use_container_width=True, key="add_pending_buy_btn"):
    try:
        _add_pending_buy_order(
            pending_order_symbol,
            float(pending_order_price),
            pending_order_qty,
            strategy_mode,
        )
        st.success(
            f"Pending buy armed for {pending_order_symbol} at or below Rs {float(pending_order_price):.2f}"
        )
        st.rerun()
    except Exception as err:
        st.error(f"Could not add pending buy: {err}")

if pm5.button("Arm Selected Symbol", use_container_width=True, key="arm_selected_symbol_btn"):
    try:
        _arm_symbol(symbol, strategy_mode)
        st.success(f"Armed {symbol} for persistent strategy watching")
        st.rerun()
    except Exception as err:
        st.error(f"Could not arm symbol: {err}")

pending_orders = _pending_buy_orders()
if pending_orders:
    pending_df = pd.DataFrame(pending_orders)[[
        "symbol", "target_price", "qty", "strategy", "created_at"
    ]].copy()
    pending_df["target_price"] = pending_df["target_price"].map(
        lambda x: f"Rs {float(x):.2f}")
    st.dataframe(pending_df, use_container_width=True)
    cancel_pending_symbol = st.selectbox(
        "Cancel pending buy",
        options=[str(item["symbol"]) for item in pending_orders],
        key="cancel_pending_symbol",
    )
    if st.button("Cancel Pending Buy", use_container_width=True, key="cancel_pending_buy_btn"):
        _remove_pending_buy_order(cancel_pending_symbol)
        st.success(f"Canceled pending buy for {cancel_pending_symbol}")
        st.rerun()
else:
    st.caption("No pending buy orders yet.")

armed_symbols = _armed_symbols()
if armed_symbols:
    armed_df = pd.DataFrame(armed_symbols)[
        ["symbol", "strategy", "armed_at"]].copy()
    st.dataframe(armed_df, use_container_width=True)
    disarm_symbol = st.selectbox(
        "Disarm watched symbol",
        options=[str(item["symbol"]) for item in armed_symbols],
        key="disarm_symbol",
    )
    if st.button("Disarm Symbol", use_container_width=True, key="disarm_symbol_btn"):
        _disarm_symbol(disarm_symbol)
        st.success(f"Disarmed {disarm_symbol}")
        st.rerun()
else:
    st.caption("No armed symbols yet.")

if "top5_df" not in st.session_state:
    st.session_state.top5_df = pd.DataFrame()
if "scan_errors" not in st.session_state:
    st.session_state.scan_errors = []
if "top_scan_requested_n" not in st.session_state:
    st.session_state.top_scan_requested_n = 5

active_session = MarketSession.from_config(cfg)
top5_now = active_session.now()
top5_cutoff = active_session.entry_cutoff
top5_allowed_now = active_session.is_before_entry_cutoff(top5_now)
has_open_positions = not top_holdings_df.empty
scan_target_n, scan_expansion_active = _scan_target_count(
    cfg=cfg,
    base_top_n=5,
    expanded_top_n=8,
    expansion_review_time=top8_review_time,
    enable_expansion=enable_top8_expansion,
)

if not top5_allowed_now:
    st.session_state.top5_df = pd.DataFrame()
    st.session_state.scan_errors = []
    st.session_state.top_scan_requested_n = 5

auto_scan_refresh = (
    auto_refresh_enabled
    and data_source == "NSE Quote API (non-Yahoo)"
    and top5_allowed_now
)

if top5_allowed_now and (
    scan_button
    or auto_scan_refresh
    or st.session_state.top5_df.empty
    or int(st.session_state.get("top_scan_requested_n", 5)) != int(scan_target_n)
):
    scan_symbols = WATCHLIST[:scan_universe_size]
    spinner_text = f"Scanning symbols for top {scan_target_n} intraday candidates..."
    if st.session_state.top5_df.empty and not scan_button:
        spinner_text = f"Running initial Top {scan_target_n} scan..."
    with st.spinner(spinner_text):
        if data_source == "NSE Quote API (non-Yahoo)":
            ranked_df, scan_errors = scan_top_stocks_nse(
                symbols=scan_symbols,
                mode=strategy_mode,
                top_n=scan_target_n,
                allow_short=cfg.allow_short,
            )
        else:
            ranked_df, scan_errors = scan_top_stocks(
                cfg=cfg,
                symbols=scan_symbols,
                mode=strategy_mode,
                top_n=scan_target_n,
                quick_period=scan_period,
            )
    ranked_df = _apply_price_band_filter(
        ranked_df=ranked_df,
        min_price=scan_min_price,
        max_price=scan_max_price,
    )
    ranked_df = _apply_cap_focus_filter(
        ranked_df=ranked_df,
        ignore_large_cap=scan_ignore_large_cap,
    )
    ranked_df = _apply_scan_expansion_filter(
        ranked_df=ranked_df,
        base_top_n=5,
        expanded_top_n=8,
        extra_min_score=top8_extra_min_score,
        expansion_active=scan_expansion_active,
    )
    if not fast_mode and not compact_ui:
        ranked_df = _apply_learning_bonus(
            ranked_df=ranked_df,
            history_df=_trade_history_df(),
            strategy_mode=strategy_mode,
        )
    ranked_df = _apply_sector_diversity_cap(
        ranked_df=ranked_df,
        max_per_sector=scan_max_per_sector,
        top_n=scan_target_n,
    )
    ranked_df = _apply_market_cap_quota(
        ranked_df=ranked_df,
        top_n=scan_target_n,
        enable_quota=scan_enable_cap_quota,
        large_quota=scan_large_quota,
        mid_quota=scan_mid_quota,
        small_quota=scan_small_quota,
    )
    ranked_df = ranked_df.head(scan_target_n).reset_index(drop=True)
    st.session_state.top5_df = ranked_df
    st.session_state.scan_errors = scan_errors
    st.session_state.top_scan_requested_n = scan_target_n

st.subheader(f"Top {scan_target_n} Intraday Candidates (Paper Testing)")
ranked_df = st.session_state.top5_df
scan_errors = st.session_state.get("scan_errors", [])

# Remove currently held symbols from Top list display/actions.
held_symbols = set(st.session_state.paper_holdings.keys())
pre_filter_count = int(len(ranked_df)) if not ranked_df.empty else 0
if not ranked_df.empty and held_symbols:
    ranked_df = ranked_df[~ranked_df["symbol"].isin(
        held_symbols)].reset_index(drop=True)
removed_count = pre_filter_count - \
    (int(len(ranked_df)) if not ranked_df.empty else 0)

if has_open_positions:
    st.caption(
        "Open positions detected. Held symbols are removed from Top list to avoid duplicates."
    )
    if removed_count > 0:
        st.caption(
            f"Filtered out {removed_count} held symbol(s) from Top candidates.")

if not top5_allowed_now:
    now_t = market_now(cfg.market_timezone).strftime("%H:%M:%S %Z")
    st.info(
        "Top 5 scanner is hidden after intraday entry cutoff. "
        f"Current: {now_t}, Entry cutoff: {top5_cutoff.strftime('%H:%M')}"
    )
elif ranked_df.empty:
    st.warning(
        "No candidates available right now. Adjust scan settings and retry.")
else:
    if not fast_mode and not compact_ui and data_source == "NSE Quote API (non-Yahoo)" and not ranked_df.empty:
        # Refresh Top list display values from live quotes before table render.
        refreshed_rows: list[dict[str, Any]] = []
        for _, r in ranked_df.iterrows():
            row_dict = dict(r)
            sym = str(row_dict.get("symbol", ""))
            if not sym:
                refreshed_rows.append(row_dict)
                continue
            try:
                q = fetch_nse_quote(sym)
                live_price = float(q.get("last_price")
                                   or row_dict.get("price", 0.0) or 0.0)
                row_dict["price"] = live_price
                row_dict["updated"] = q.get(
                    "updated") or row_dict.get("updated", "")

                live_plan = _strategy_plan_from_nse_quote(
                    quote=q,
                    cfg=cfg,
                    strategy_mode=strategy_mode,
                )
                if live_plan:
                    row_dict["expected_entry"] = float(
                        live_plan.get("entry_price") or row_dict.get(
                            "expected_entry", 0.0) or 0.0
                    )
                    row_dict["trigger_hit"] = bool(
                        live_plan.get("trigger_hit") if live_plan.get(
                            "trigger_hit") is not None else row_dict.get("trigger_hit", False)
                    )
                    row_dict["planned_sl"] = float(
                        live_plan.get("sl") or row_dict.get(
                            "planned_sl", 0.0) or 0.0
                    )
                    row_dict["planned_tp"] = float(
                        live_plan.get("tp") or row_dict.get(
                            "planned_tp", 0.0) or 0.0
                    )
                    row_dict["trigger_reason"] = str(
                        live_plan.get("reason") or row_dict.get(
                            "trigger_reason", "")
                    )

                _update_paper_price(sym, live_price)
            except Exception:
                pass
            refreshed_rows.append(row_dict)

        if refreshed_rows:
            ranked_df = pd.DataFrame(refreshed_rows)

    if not fast_mode and not compact_ui:
        learning_profile = _learning_symbol_profile(
            history_df=_trade_history_df(),
            strategy_mode=strategy_mode,
        )
        if not learning_profile.empty:
            active_symbols = set(ranked_df["symbol"].astype(
                str).tolist()) if "symbol" in ranked_df.columns else set()
            suggestion_df = learning_profile[
                ~learning_profile["symbol"].astype(str).isin(active_symbols)
            ].head(5).copy()
            if not suggestion_df.empty:
                show_suggestion_df = suggestion_df[[
                    "symbol", "trades", "win_rate", "avg_pnl", "learning_bias"
                ]].copy()
                show_suggestion_df["win_rate"] = show_suggestion_df["win_rate"].map(
                    lambda x: f"{float(x) * 100:.1f}%")
                show_suggestion_df["avg_pnl"] = show_suggestion_df["avg_pnl"].map(
                    lambda x: f"Rs {float(x):.2f}")
                st.markdown(
                    "**Learning Agent Suggestions (Additional Symbols)**")
                st.caption(
                    "These are not in the current Top list but historically performed better in your closed trades."
                )
                st.dataframe(show_suggestion_df,
                             use_container_width=True, hide_index=True)

    if scan_expansion_active:
        st.caption(
            f"Expansion active after {top8_review_time.strftime('%H:%M')}: no buy happened yet today, so the list widened to Top 8. "
            f"Ranks 6-8 must have score >= {top8_extra_min_score:.1f}."
        )
    show_cols = [
        "symbol",
        "price",
        "score",
        "action",
        "bias",
        "range_pct",
        "expected_entry",
        "trigger_hit",
        "updated",
    ]
    show_df = ranked_df[show_cols].copy()
    show_df["price"] = show_df["price"].map(lambda x: f"Rs {x:.2f}")
    show_df["expected_entry"] = show_df["expected_entry"].map(
        lambda x: f"Rs {x:.2f}")
    show_df["trigger_hit"] = show_df["trigger_hit"].map(
        {True: "READY", False: "WAIT"})
    st.dataframe(show_df, use_container_width=True)

    pick_options = [
        f"{r.symbol} | Rs {r.price:.2f} | {r.action}" for r in ranked_df.itertuples(index=False)]
    picked = st.selectbox(
        "Pick stock from Top 5 (uses current scanned price)", pick_options)
    picked_symbol = picked.split(" | ")[0]
    if picked_symbol:
        cfg.symbol = picked_symbol
        symbol = picked_symbol
        st.success(f"Selected for detailed local testing: {symbol}")

    st.subheader("Top 5 Strategy Quick Actions")
    st.caption(
        "Buy becomes enabled only when strategy trigger is READY and no open holding exists for that stock. "
        "Expected profit/loss below is estimated from planned TP/SL."
    )
    top5_strategy_qty = int(st.number_input(
        "Top 5 strategy qty",
        min_value=1,
        value=1,
        step=1,
        key="top5_strategy_qty",
    ))

    st.markdown("**Top 5 Auto-Buy (based on available cash)**")
    combo_mode = st.selectbox(
        "Auto-buy combination",
        ["Top 3 from Top 5", "All 5 from Top 5"],
        index=0,
        key="top5_combo_mode",
    )

    available_cash_now = float(st.session_state.paper_cash)
    st.caption(
        f"How much do I have? Available cash: Rs {available_cash_now:.2f}"
    )

    # Build candidate list from current Top 5 rows using trigger state and no-open-position rule.
    auto_candidates: list[dict[str, Any]] = []
    for _, crow in ranked_df.reset_index(drop=True).iterrows():
        c_symbol = str(crow["symbol"])
        c_price = float(crow["price"])
        c_trigger = bool(crow.get("trigger_hit", False))
        c_score = float(crow.get("score", 0.0) or 0.0)
        c_holding = st.session_state.paper_holdings.get(
            c_symbol, {"qty": 0.0, "avg_price": 0.0}
        )
        c_holding_qty = int(float(c_holding.get("qty", 0.0)))

        # In NSE mode, refresh trigger with latest quote for better auto-buy accuracy.
        if data_source == "NSE Quote API (non-Yahoo)":
            try:
                c_quote = fetch_nse_quote(c_symbol)
                c_price = float(c_quote.get("last_price") or c_price)
                c_live_plan = _strategy_plan_from_nse_quote(
                    quote=c_quote,
                    cfg=cfg,
                    strategy_mode=strategy_mode,
                )
                c_trigger = bool(c_live_plan.get("trigger_hit"))
            except Exception:
                pass

        if c_trigger and c_holding_qty <= 0 and c_price > 0:
            est_value = float(top5_strategy_qty) * c_price
            est_charges = _intraday_charges("BUY", est_value)
            est_block = est_value * INTRADAY_MARGIN_RATE + est_charges
            auto_candidates.append(
                {
                    "symbol": c_symbol,
                    "price": c_price,
                    "score": c_score,
                    "est_block": float(est_block),
                }
            )

    auto_candidates = sorted(
        auto_candidates, key=lambda x: x["score"], reverse=True)
    combo_size = 3 if combo_mode == "Top 3 from Top 5" else 5
    selected_candidates = auto_candidates[:combo_size]
    est_total_needed = float(sum(x["est_block"] for x in selected_candidates))

    st.caption(
        f"Triggered candidates: {len(auto_candidates)} | Selected combo size: {len(selected_candidates)} | "
        f"Estimated margin+charges needed: Rs {est_total_needed:.2f}"
    )

    if st.button("Auto Buy Selected Combination", use_container_width=True, key="top5_auto_buy_btn"):
        if not selected_candidates:
            st.warning("No eligible triggered stocks right now for auto-buy.")
        else:
            bought_msgs: list[str] = []
            skipped_msgs: list[str] = []
            for cand in selected_candidates:
                sym = str(cand["symbol"])
                px = float(cand["price"])
                est_block = float(cand["est_block"])
                live_cash = float(st.session_state.paper_cash)

                if live_cash < est_block:
                    skipped_msgs.append(
                        f"{sym}: insufficient cash (need Rs {est_block:.2f}, have Rs {live_cash:.2f})"
                    )
                    continue

                try:
                    _execute_paper_order(
                        symbol=sym,
                        side="BUY",
                        qty=top5_strategy_qty,
                        price=px,
                        note=f"Top5 Auto BUY ({strategy_mode})",
                    )
                    _update_paper_price(sym, px)
                    bought_msgs.append(f"{sym} @ Rs {px:.2f}")
                except Exception as err:
                    skipped_msgs.append(f"{sym}: {err}")

            if bought_msgs:
                st.success("Auto-buy executed: " + ", ".join(bought_msgs))
            if skipped_msgs:
                st.warning("Auto-buy skipped: " + " | ".join(skipped_msgs))
            if bought_msgs:
                st.rerun()

    for idx, row in ranked_df.reset_index(drop=True).iterrows():
        row_symbol = str(row["symbol"])
        row_price = float(row["price"])
        row_expected = float(row.get("expected_entry", 0.0) or 0.0)
        row_trigger = bool(row.get("trigger_hit", False))
        row_sl = float(row.get("planned_sl", 0.0) or 0.0)
        row_tp = float(row.get("planned_tp", 0.0) or 0.0)
        row_reason = str(row.get("trigger_reason", ""))
        row_quote_ts = str(row.get("updated", ""))

        if data_source == "NSE Quote API (non-Yahoo)":
            try:
                live_quote = fetch_nse_quote(row_symbol)
                row_price = float(live_quote.get("last_price") or row_price)
                live_plan = _strategy_plan_from_nse_quote(
                    quote=live_quote,
                    cfg=cfg,
                    strategy_mode=strategy_mode,
                )
                row_expected = float(live_plan.get(
                    "entry_price") or row_expected)
                row_trigger = bool(live_plan.get("trigger_hit"))
                row_sl = float(live_plan.get("sl") or row_sl)
                row_tp = float(live_plan.get("tp") or row_tp)
                row_reason = str(live_plan.get("reason") or row_reason)
            except Exception:
                pass

        _update_paper_price(row_symbol, row_price)
        row_quote_ts = st.session_state.get("paper_price_updates", {}).get(
            row_symbol,
            row_quote_ts,
        )

        top5_plan = {
            "entry_price": row_expected,
            "trigger_hit": row_trigger,
            "sl": row_sl,
            "tp": row_tp,
        }
        _update_strategy_tracking(
            symbol=row_symbol,
            strategy_mode=strategy_mode,
            plan=top5_plan,
            current_price=row_price,
            tz_name=cfg.market_timezone,
        )

        row_holding = st.session_state.paper_holdings.get(
            row_symbol, {"qty": 0.0, "avg_price": 0.0})
        row_holding_qty = int(float(row_holding.get("qty", 0.0)))
        row_avg_price = float(row_holding.get("avg_price", 0.0))

        row_buy_disabled = (not row_trigger) or (row_holding_qty > 0)
        buy_enabled = not row_buy_disabled

        row_exit_ready = False
        row_exit_reason = "No holding to exit"
        if row_holding_qty > 0:
            position_sl = row_avg_price * (1 - cfg.stop_loss_pct)
            position_tp = row_avg_price * (1 + cfg.take_profit_pct)
            if row_price <= position_sl:
                row_exit_ready = True
                row_exit_reason = "Stop-loss reached"
            elif row_price >= position_tp:
                row_exit_ready = True
                row_exit_reason = "Target reached"
            elif _in_square_off_window(cfg):
                row_exit_ready = True
                row_exit_reason = "Square-off time reached"
            else:
                row_exit_reason = "Waiting for SL/TP or square-off"

        st.markdown(f"**{row_symbol}**")
        rc1, rc2, rc3, rc4, rc5 = st.columns([2, 2, 2, 1.4, 1.4])

        risk_per_share = max(0.0, row_expected - row_sl)
        reward_per_share = max(0.0, row_tp - row_expected)
        rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0.0
        expected_profit = reward_per_share * top5_strategy_qty
        expected_loss = risk_per_share * top5_strategy_qty

        rc1.caption(
            f"Current: Rs {row_price:.2f} | Expected: Rs {row_expected:.2f} | Trigger: {'READY' if row_trigger else 'WAIT'}"
        )
        rc2.caption(
            f"SL: Rs {row_sl:.2f} | TP: Rs {row_tp:.2f} | Holding: {row_holding_qty}"
        )
        rc3.caption(f"Logic: {row_reason}")
        rc3.caption(f"Last Quote: {row_quote_ts}")

        st.caption(
            f"Why buy: {row_reason} | When to enter: at/above Rs {row_expected:.2f} during entry window | "
            f"Expected profit: Rs {expected_profit:.2f} | Expected loss: Rs {expected_loss:.2f} | R:R {rr:.2f}"
        )

        if rc4.button(
            "Buy Now",
            key=f"top5_buy_{idx}_{row_symbol}",
            disabled=row_buy_disabled,
            use_container_width=True,
        ):
            try:
                _execute_paper_order(
                    symbol=row_symbol,
                    side="BUY",
                    qty=top5_strategy_qty,
                    price=row_price,
                    note=f"Top5 Strategy BUY ({strategy_mode})",
                )
                _update_paper_price(row_symbol, row_price)
                st.success(
                    f"Bought {top5_strategy_qty} {row_symbol} @ Rs {row_price:.2f}"
                )
                st.rerun()
            except Exception as err:
                st.error(f"Buy failed for {row_symbol}: {err}")

        if rc5.button(
            "Exit",
            key=f"top5_exit_{idx}_{row_symbol}",
            disabled=not row_exit_ready,
            use_container_width=True,
        ):
            try:
                _execute_paper_order(
                    symbol=row_symbol,
                    side="SELL",
                    qty=row_holding_qty,
                    price=row_price,
                    note=f"Top5 Strategy EXIT ({strategy_mode})",
                )
                _update_paper_price(row_symbol, row_price)
                st.success(
                    f"Exited {row_holding_qty} {row_symbol} @ Rs {row_price:.2f}"
                )
                st.rerun()
            except Exception as err:
                st.error(f"Exit failed for {row_symbol}: {err}")

        if buy_enabled:
            reason_text = "Buy enabled: strategy trigger reached"
        elif row_holding_qty > 0:
            reason_text = "Buy disabled: position already open"
        else:
            reason_text = "Buy disabled: waiting for entry trigger"
        st.caption(
            f"{reason_text} | Exit: {row_exit_reason}"
        )
        st.divider()

if scan_errors:
    with st.expander("Scanner Errors (skipped symbols)", expanded=False):
        for err in scan_errors[:10]:
            st.caption(err)

# Additional risk bucket section is intentionally disabled for now.

st.divider()
st.subheader("Quick Price Snapshot")
current_symbol_price = float(
    st.session_state.paper_prices.get(symbol, 0.0) or 0.0)
if data_source == "NSE Quote API (non-Yahoo)":
    try:
        quote = fetch_nse_quote(symbol)
        current_symbol_price = float(
            quote.get("last_price") or current_symbol_price)
        _update_paper_price(symbol, current_symbol_price)
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("Symbol", symbol)
        q2.metric("Last", f"Rs {current_symbol_price:.2f}")
        q3.metric("Change %", f"{float(quote.get('pchange') or 0.0):.2f}%")
        q4.metric("VWAP", f"Rs {float(quote.get('vwap') or 0.0):.2f}")
    except Exception as err:
        st.caption(f"Snapshot unavailable for {symbol}: {err}")
else:
    st.caption("Quick snapshot is optimized for NSE data source.")

st.divider()
st.subheader("Dummy Portfolio (Paper Trading)")

holdings_df, cash, market_value, total_equity = _portfolio_snapshot()
invested_value = float(
    holdings_df["cost_value"].sum()) if not holdings_df.empty else 0.0
unrealized_pnl = float(
    holdings_df["unrealized_pnl"].sum()) if not holdings_df.empty else 0.0
realized_pnl = float(st.session_state.paper_realized_pnl)
total_charges = float(st.session_state.paper_total_charges)

st.markdown("**Bought Stocks Profit/Loss**")
if holdings_df.empty:
    st.info("No bought stocks yet.")
else:
    entry_charge_map = _open_position_entry_charges()
    show_bought = holdings_df.rename(
        columns={
            "avg_price": "bought_price",
            "ltp": "current_price",
            "unrealized_pnl": "profit_loss",
            "margin_used": "invested_margin",
        }
    )[["symbol", "bought_price", "current_price", "cost_value", "invested_margin", "profit_loss"]].copy()
    show_bought = show_bought.rename(
        columns={"cost_value": "position_notional"})
    show_bought["entry_charges"] = show_bought["symbol"].map(
        lambda s: float(entry_charge_map.get(str(s), 0.0))
    )
    show_bought["actual_capital_used"] = (
        show_bought["invested_margin"].astype(float)
        + show_bought["entry_charges"].astype(float)
    )
    show_bought = show_bought[
        [
            "symbol",
            "bought_price",
            "current_price",
            "position_notional",
            "invested_margin",
            "entry_charges",
            "actual_capital_used",
            "profit_loss",
        ]
    ].copy()
    show_bought = show_bought.sort_values(by="symbol")
    total_invested_margin = float(
        show_bought["invested_margin"].astype(float).sum())
    total_actual_capital = float(
        show_bought["actual_capital_used"].astype(float).sum())
    for c in [
        "bought_price",
        "current_price",
        "position_notional",
        "invested_margin",
        "entry_charges",
        "actual_capital_used",
        "profit_loss",
    ]:
        show_bought[c] = show_bought[c].map(lambda x: f"Rs {float(x):.2f}")
    st.dataframe(show_bought, use_container_width=True)
    st.caption(
        f"Total invested margin: Rs {total_invested_margin:.2f} | Total actual capital used (margin + entry charges): Rs {total_actual_capital:.2f}"
    )

    st.markdown("**Quick Exit from Holdings**")
    for _, hrow in holdings_df.sort_values(by="symbol").iterrows():
        hs = str(hrow["symbol"])
        hq = int(float(hrow.get("qty", 0.0)))
        hltp = float(hrow.get("ltp", 0.0))
        havg = float(hrow.get("avg_price", 0.0))
        exit_price = hltp if hltp > 0 else havg

        holdings_exit_ready = False
        holdings_exit_reason = "Waiting for SL/TP or square-off"
        if hq <= 0:
            holdings_exit_reason = "No holding to exit"
        else:
            position_sl = havg * (1 - cfg.stop_loss_pct)
            position_tp = havg * (1 + cfg.take_profit_pct)
            if exit_price <= position_sl:
                holdings_exit_ready = True
                holdings_exit_reason = "Stop-loss reached"
            elif exit_price >= position_tp:
                holdings_exit_ready = True
                holdings_exit_reason = "Target reached"
            elif _in_square_off_window(cfg):
                holdings_exit_ready = True
                holdings_exit_reason = "Square-off time reached"

        qc1, qc2, qc3, qc4 = st.columns([1.2, 1.6, 1.6, 2.6])
        if qc1.button(
            "Exit",
            key=f"holdings_exit_{hs}",
            disabled=not holdings_exit_ready,
            use_container_width=True,
        ):
            try:
                _execute_paper_order(
                    symbol=hs,
                    side="SELL",
                    qty=hq,
                    price=exit_price,
                    note=f"Holdings Quick Exit ({strategy_mode})",
                )
                _update_paper_price(hs, float(exit_price))
                st.success(f"Exited {hq} {hs} @ Rs {exit_price:.2f}")
                st.rerun()
            except Exception as err:
                st.error(f"Exit failed for {hs}: {err}")
        qc2.caption(f"{hs} | Qty: {hq}")
        qc3.caption(
            f"Avg: Rs {havg:.2f} | TP: Rs {position_tp:.2f} | SL: Rs {position_sl:.2f}")
        qc4.caption(
            f"LTP: Rs {hltp:.2f} | {'✅ Exit enabled' if holdings_exit_ready else '⏳ Exit disabled'}: {holdings_exit_reason}"
        )

    price_updates = st.session_state.get("paper_price_updates", {})
    if price_updates:
        latest_update = max(price_updates.values())
        st.caption(
            f"Current prices come from {data_source}. Last portfolio price refresh: {latest_update}. "
            "Broker feeds like Zerodha Kite can differ slightly due to quote timing and feed source."
        )

order_symbols = sorted(
    set(WATCHLIST + [symbol] + list(st.session_state.paper_holdings.keys())))
oc1, oc2, oc3, oc4 = st.columns([2, 1, 1, 2])
order_symbol = oc1.selectbox(
    "Order symbol",
    options=order_symbols,
    index=order_symbols.index(symbol) if symbol in order_symbols else 0,
)
order_side = oc2.selectbox("Side", ["BUY", "SELL"])
order_qty = int(oc3.number_input("Qty", min_value=1, value=1, step=1))
default_price = float(st.session_state.paper_prices.get(
    order_symbol, current_symbol_price or 0.0))
order_price = oc4.number_input(
    "Execution price",
    min_value=0.01,
    value=max(0.01, round(default_price, 2)),
    step=0.05,
)
order_note = st.text_input("Order note (optional)", value="")

if st.button("Place Dummy Order", use_container_width=True):
    try:
        _execute_paper_order(
            symbol=order_symbol,
            side=order_side,
            qty=order_qty,
            price=float(order_price),
            note=order_note,
        )
        _update_paper_price(order_symbol, float(order_price))
        st.success(
            f"Dummy order executed: {order_side} {order_qty} {order_symbol} @ Rs {float(order_price):.2f}"
        )
        st.rerun()
    except Exception as err:
        st.error(f"Order rejected: {err}")

hc1, hc2 = st.columns(2)
with hc1:
    st.markdown("**Current Holdings**")
    if holdings_df.empty:
        st.info("No holdings yet. Place a BUY order to start paper testing.")
    elif compact_ui:
        st.caption(
            "Compact UI: holdings table hidden. Disable Compact UI in sidebar to view details.")
    else:
        show_holdings = holdings_df.copy()
        for c in ["avg_price", "ltp", "cost_value", "market_value", "unrealized_pnl"]:
            show_holdings[c] = show_holdings[c].map(lambda x: f"Rs {x:.2f}")
        st.dataframe(show_holdings, use_container_width=True)

with hc2:
    st.markdown("**Dummy Trade Log**")
    trade_log = pd.DataFrame(st.session_state.paper_trade_log)
    if trade_log.empty:
        st.info("No dummy trades yet.")
    elif compact_ui:
        st.caption(
            "Compact UI: trade log table hidden. Disable Compact UI in sidebar to view details.")
    else:
        show_log = trade_log.copy()
        for c in ["price", "value", "cash_after"]:
            show_log[c] = show_log[c].map(lambda x: f"Rs {x:.2f}")
        st.dataframe(show_log.sort_index(ascending=False),
                     use_container_width=True)

st.divider()
st.subheader("History and Learning")

if fast_mode or compact_ui:
    st.caption(
        "Fast/Compact mode is ON: detailed history analytics are skipped to improve load speed."
    )

history_df = pd.DataFrame() if (fast_mode or compact_ui) else _trade_history_df()
if history_df.empty:
    st.info("No history yet. Place paper trades to build learning insights.")
else:
    summary_df = history_df.copy()
    for col in ["realized_pnl", "charges", "qty", "price"]:
        if col not in summary_df.columns:
            summary_df[col] = 0.0
        summary_df[col] = summary_df[col].astype(float)

    closed_df = summary_df[summary_df["side"] == "SELL"].copy()
    wins = closed_df[closed_df["realized_pnl"] > 0]
    losses = closed_df[closed_df["realized_pnl"] < 0]
    gross_realized = float(
        closed_df["realized_pnl"].sum()) if not closed_df.empty else 0.0
    total_charges_summary = float(summary_df["charges"].sum())
    net_realized = gross_realized - total_charges_summary
    win_rate_summary = float(
        (closed_df["realized_pnl"] > 0).mean() * 100) if not closed_df.empty else 0.0
    avg_trade_pnl = float(
        closed_df["realized_pnl"].mean()) if not closed_df.empty else 0.0
    avg_qty = float(summary_df["qty"].mean()) if not summary_df.empty else 0.0

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Orders", f"{len(summary_df)}")
    k2.metric("Closed Trades", f"{len(closed_df)}")
    k3.metric("Win Rate", f"{win_rate_summary:.1f}%")
    k4.metric("Net PnL", f"Rs {net_realized:.2f}")
    k5.metric("Avg Trade PnL", f"Rs {avg_trade_pnl:.2f}")
    k6.metric("Avg Qty", f"{avg_qty:.1f}")

    def _extract_strategy_tag(note: str) -> str:
        text = str(note)
        if "(" in text and ")" in text:
            return text.split("(")[-1].split(")")[0].strip() or "Unknown"
        return "Unknown"

    summary_df["strategy"] = summary_df.get(
        "note", "").astype(str).map(_extract_strategy_tag)
    strat_base = summary_df[summary_df["side"] == "SELL"].copy()
    if not strat_base.empty:
        strat_summary = strat_base.groupby("strategy", as_index=False).agg(
            trades=("side", "count"),
            win_rate=("realized_pnl", lambda s: float((s > 0).mean() * 100)),
            avg_pnl=("realized_pnl", "mean"),
            total_pnl=("realized_pnl", "sum"),
            avg_exit_price=("price", "mean"),
        )
        strat_summary["win_rate"] = strat_summary["win_rate"].map(
            lambda x: round(float(x), 1))
        for c in ["avg_pnl", "total_pnl", "avg_exit_price"]:
            strat_summary[c] = strat_summary[c].map(
                lambda x: round(float(x), 2))
        st.markdown("**Trade Parameter Summary by Strategy**")
        st.dataframe(strat_summary.sort_values(
            "total_pnl", ascending=False), use_container_width=True, hide_index=True)

    symbol_summary = summary_df[summary_df["side"] == "SELL"].groupby("symbol", as_index=False).agg(
        trades=("side", "count"),
        total_pnl=("realized_pnl", "sum"),
        avg_pnl=("realized_pnl", "mean"),
        avg_exit_price=("price", "mean"),
    )
    if not symbol_summary.empty:
        for c in ["total_pnl", "avg_pnl", "avg_exit_price"]:
            symbol_summary[c] = symbol_summary[c].map(
                lambda x: round(float(x), 2))
        st.markdown("**Trade Parameter Summary by Symbol**")
        st.dataframe(symbol_summary.sort_values(
            "total_pnl", ascending=False), use_container_width=True, hide_index=True)

    # Beginner-friendly explanation: pair each BUY with its corresponding SELL.
    explain_rows: list[dict[str, Any]] = []
    open_buys: dict[str, list[dict[str, Any]]] = {}
    ordered = summary_df.sort_values("timestamp_ist").to_dict("records")
    for row in ordered:
        sym = str(row.get("symbol", ""))
        side = str(row.get("side", "")).upper()
        qty = int(float(row.get("qty", 0.0) or 0.0))
        price = float(row.get("price", 0.0) or 0.0)
        charges = float(row.get("charges", 0.0) or 0.0)
        margin = float(row.get("margin", 0.0) or 0.0)

        if side == "BUY":
            open_buys.setdefault(sym, []).append(
                {
                    "time": str(row.get("timestamp_ist", "")),
                    "qty": qty,
                    "price": price,
                    "charges": charges,
                    "margin": margin,
                }
            )
        elif side == "SELL" and sym in open_buys and open_buys[sym]:
            buy_leg = open_buys[sym].pop(0)
            gross_pnl = float(row.get("realized_pnl", 0.0) or 0.0)
            total_charges_trade = float(buy_leg["charges"]) + charges
            net_pnl = gross_pnl - total_charges_trade
            explain_rows.append(
                {
                    "symbol": sym,
                    "qty": qty,
                    "buy_price": buy_leg["price"],
                    "sell_price": price,
                    "capital_used_at_entry": float(buy_leg["margin"]) + float(buy_leg["charges"]),
                    "gross_pnl": gross_pnl,
                    "total_charges": total_charges_trade,
                    "net_pnl": net_pnl,
                    "entry_time": buy_leg["time"],
                    "exit_time": str(row.get("timestamp_ist", "")),
                }
            )

    if explain_rows:
        explain_df = pd.DataFrame(explain_rows)
        st.markdown("**Beginner Trade Explanation (Per Completed Trade)**")
        show_explain = explain_df.copy()
        for c in ["buy_price", "sell_price", "capital_used_at_entry", "gross_pnl", "total_charges", "net_pnl"]:
            show_explain[c] = show_explain[c].map(
                lambda x: f"Rs {float(x):.2f}")
        st.dataframe(show_explain.sort_values(
            "exit_time", ascending=False), use_container_width=True, hide_index=True)

    show_history = history_df.copy()
    for c in ["price", "value", "charges", "realized_pnl", "cash_after"]:
        if c in show_history.columns:
            show_history[c] = show_history[c].astype(
                float).map(lambda x: f"Rs {x:.2f}")
    st.markdown("**Order History (Persistent)**")
    st.dataframe(show_history.sort_index(
        ascending=False), use_container_width=True)

    if "timestamp_ist" in history_df.columns:
        daily = history_df.copy()
        daily["trade_date"] = daily["timestamp_ist"].astype(
            str).str.slice(0, 10)
        for col in ["realized_pnl", "charges"]:
            if col not in daily.columns:
                daily[col] = 0.0
            daily[col] = daily[col].astype(float)

        daily_summary = daily.groupby("trade_date", as_index=False).agg(
            realized_pnl=("realized_pnl", "sum"),
            charges=("charges", "sum"),
            trades=("side", "count"),
        )
        daily_summary["net_pnl"] = daily_summary["realized_pnl"] - \
            daily_summary["charges"]

        show_daily = daily_summary.copy()
        for c in ["realized_pnl", "charges", "net_pnl"]:
            show_daily[c] = show_daily[c].map(lambda x: f"Rs {float(x):.2f}")

        st.markdown("**End-of-Day PnL Summary**")
        st.dataframe(show_daily.sort_values(
            "trade_date", ascending=False), use_container_width=True)

    st.markdown("**Learning Insights**")
    for tip in _learning_summary(history_df):
        st.caption(f"- {tip}")

    st.markdown("**Strategy-wise Comparison (ORB / VWAP / Reversal)**")
    strat_df = _strategy_learning_summary(history_df)
    if strat_df.empty:
        st.info("No closed trades with strategy labels yet.")
    else:
        st.dataframe(strat_df, use_container_width=True, hide_index=True)

    _export_daily_pnl(history_df)
    st.caption(f"Daily PnL auto-exported to {DAILY_PNL_FILE}")

st.divider()
st.caption("Paper-testing only. No real money execution in this app.")

# Schedule browser refresh at end-of-page render so the countdown starts only after full paint.
if auto_refresh_enabled and data_source == "NSE Quote API (non-Yahoo)":
    _auto_refresh(auto_refresh_seconds, hard_reload_fallback=True)
