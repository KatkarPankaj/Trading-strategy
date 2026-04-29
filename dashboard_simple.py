"""
Simple Budget-Based Trading Simulator (Paper Trading)
Run with: streamlit run dashboard_simple.py --server.port 8507
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytz
import streamlit as st
import streamlit.components.v1 as components

try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None


IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
ENTRY_CUTOFF = time(13, 30)
SQUARE_OFF = time(15, 15)

WATCHLIST = [
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
]

STATE_FILE = Path("outputs") / "simple_paper_state.json"
CLEAN_CLOSED_TRADES_FILE = Path("outputs") / "clean_closed_trades_light.csv"

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

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
    )
except Exception:
    train_market_learning_model = None
    get_symbol_quality_score = None
    MarketLearningModel = None


def _read_saved_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _auto_export_clean_closed_trades() -> tuple[int, str | None]:
    if collect_clean_closed_trades is None:
        return 0, "Optimizer module unavailable; clean closed-trade export skipped."

    # Fresh reset state can leave an empty file briefly; treat as no trades yet.
    if not STATE_FILE.exists() or STATE_FILE.stat().st_size == 0:
        return 0, None

    try:
        closed_trades_df, _ = collect_clean_closed_trades(
            STATE_FILE,
            lookback_trades=0,
        )
        CLEAN_CLOSED_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        closed_trades_df.to_csv(CLEAN_CLOSED_TRADES_FILE, index=False)
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
            trade_file=STATE_FILE,
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
    components.html(
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
        """,
        height=0,
        width=0,
    )


def _to_nse_symbol(symbol: str) -> str:
    return str(symbol).split(".")[0].upper()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_nse_quote(symbol: str) -> dict[str, float]:
    if nsefetch is None:
        raise ValueError(
            "nsepython is not installed. Run: pip install nsepython")

    nse_symbol = _to_nse_symbol(symbol)
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

    return {
        "symbol": symbol,
        "price": price,
        "vwap": vwap,
        "pchange": pchange,
        "range_pct": range_pct,
    }


def _intraday_charges(side: str, turnover: float) -> float:
    brokerage = min(turnover * 0.0003, 20.0)
    exchange_txn = turnover * 0.0000325
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    stt = turnover * 0.00025 if side == "SELL" else 0.0
    return brokerage + exchange_txn + sebi + gst + stamp + stt


def _init_state(starting_capital: float) -> None:
    if "s_cash" in st.session_state:
        return

    if STATE_FILE.exists():
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
            return
        except Exception:
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


def _save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _today_entry_count() -> int:
    today = ist_now().strftime("%Y-%m-%d")
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

    market_open_dt = datetime.combine(now_naive.date(), MARKET_OPEN)
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
        "last_updated": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
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


def _agent_tuning_plan(
    base_min_buy_score: float,
    base_min_short_score: float,
    base_tp_pct: float,
    base_idle_buy_fallback_minutes: int,
    base_reentry_cooldown_minutes: int,
    base_min_expected_profit_per_trade: float,
    learning_memory: dict[str, Any],
    market_research: dict[str, Any],
) -> dict[str, Any]:
    adj_buy = 0.0
    adj_short = 0.0
    tp_mult = 1.0
    suggested_idle_fallback = int(base_idle_buy_fallback_minutes)
    suggested_reentry_cooldown = int(base_reentry_cooldown_minutes)
    suggested_min_expected_profit = float(base_min_expected_profit_per_trade)
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
                suggested_min_expected_profit = min(
                    500000.0, suggested_min_expected_profit * 1.2)
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
        suggested_min_expected_profit = min(
            500000.0, suggested_min_expected_profit * 1.1)
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
        "effective_min_buy_score": float(min(100.0, max(0.0, base_min_buy_score + adj_buy))),
        "effective_min_short_score": float(min(100.0, max(0.0, base_min_short_score + adj_short))),
        "effective_tp_pct": float(min(0.06, max(0.003, base_tp_pct * tp_mult))),
        "suggested_idle_buy_fallback_minutes": int(suggested_idle_fallback),
        "suggested_reentry_cooldown_minutes": int(suggested_reentry_cooldown),
        "suggested_min_expected_profit_per_trade": float(suggested_min_expected_profit),
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


def _max_feasible_expected_profit(
    total_capital: float,
    cash_now: float,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    max_qty_per_trade: int,
    max_price: float,
    tp_pct: float,
) -> float:
    order_cap = _max_feasible_order_value(
        total_capital=total_capital,
        cash_now=cash_now,
        max_symbol_allocation_pct=max_symbol_allocation_pct,
        max_total_deployment_pct=max_total_deployment_pct,
        max_qty_per_trade=max_qty_per_trade,
        max_price=max_price,
    )
    qty_cap = max(1, int(max_qty_per_trade))
    price_cap = max(1.0, float(max_price))
    tp_val = max(0.0001, float(tp_pct))
    return float(min(order_cap * tp_val, qty_cap * price_cap * tp_val))


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
    else:
        return

    st.session_state.s_charges += ch
    st.session_state.s_prices[symbol] = float(price)
    st.session_state.s_log.append(
        {
            "ts": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
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
    now = ist_now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    return MARKET_OPEN <= t <= ENTRY_CUTOFF


@st.cache_data(ttl=5, show_spinner=False)
def _scan_watchlist(
    symbols: tuple[str, ...],
    min_price: float,
    max_price: float,
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for sym in symbols:
        try:
            q = fetch_nse_quote(sym)
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

            buy_score += min(45.0, max(0.0, pchange) * 6.0)
            buy_score += min(35.0, above * 20.0)
            buy_score += min(20.0, range_pct * 2.0)
            buy_ready = price > vwap and pchange > 0.25 and range_pct > 0.5

            sell_score += min(45.0, max(0.0, -pchange) * 6.0)
            sell_score += min(35.0, below * 20.0)
            sell_score += min(20.0, range_pct * 2.0)
            sell_ready = price < vwap and pchange < -0.25 and range_pct > 0.5

            rows.append(
                {
                    "symbol": sym,
                    "price": round(price, 2),
                    "buy_score": round(buy_score, 2),
                    "buy_signal": "READY" if buy_ready else "WAIT",
                    "sell_score": round(sell_score, 2),
                    "sell_signal": "READY" if sell_ready else "WAIT",
                    "pchange": round(pchange, 2),
                    "updated": ist_now().strftime("%H:%M:%S"),
                }
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")

    return pd.DataFrame(rows), errors


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
    min_expected_profit_per_trade: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    total_capital = float(st.session_state.get("s_start", 0.0) or 0.0)
    cash_now = float(st.session_state.get("s_cash", 0.0) or 0.0)
    risk_budget = total_capital * (float(risk_pct) / 100.0)
    symbol_cap_value = total_capital * \
        (float(max_symbol_allocation_pct) / 100.0)
    deploy_cap_value = total_capital * \
        (float(max_total_deployment_pct) / 100.0)
    max_qty_limit = max(1, int(max_qty_per_trade))
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

    # Unfiltered sell shortlist is reserved for long-exit signal handling.
    sell_exit_df = all_df.sort_values(["sell_score", "pchange"], ascending=[
        False, True]).head(5).reset_index(drop=True)

    buy_candidates = all_df.sort_values(["buy_score", "pchange"], ascending=[
                                        False, False]).reset_index(drop=True)
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

        qty_upper = max(0, min(
            qty_by_risk,
            qty_by_cash,
            qty_by_symbol_cap,
            qty_by_total_cap,
            qty_by_slot_budget,
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

        effective_min_expected_profit = min(
            float(min_expected_profit_per_trade),
            max(50.0, float(slot_budget) * float(tp_pct)),
            max(50.0, float(remaining_symbol_cap) * float(tp_pct)),
            max(50.0, float(remaining_total_cap) * float(tp_pct)),
        )
        qty_floor_by_profit = max(1, int(math.ceil(
            float(effective_min_expected_profit) /
            max(price * float(tp_pct), 1e-6)
        )))
        qty_lower = max(qty_floor_by_value, qty_floor_by_profit)
        if qty_upper <= 0 or qty_upper < qty_lower:
            continue

        est_value = float(qty_upper) * float(price)
        est_ch = _intraday_charges("BUY", est_value)
        if buy_cash_left < (est_value + est_ch):
            continue
        expected_profit = float(est_value) * float(tp_pct)
        if expected_profit < float(effective_min_expected_profit):
            continue

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

    sell_candidates = all_df.sort_values(["sell_score", "pchange"], ascending=[
                                         False, True]).reset_index(drop=True)
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

        qty_upper = max(0, min(
            qty_by_risk,
            qty_by_margin,
            qty_by_symbol_cap,
            qty_by_total_cap,
            qty_by_slot_budget,
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
            q = fetch_nse_quote(sym)
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
    max_qty_per_trade: int,
    symbols: list[str],
    min_price: float,
    max_price: float,
    max_symbol_allocation_pct: float,
    max_total_deployment_pct: float,
    min_order_value: float,
    min_expected_profit_per_trade: float,
    idle_buy_fallback_minutes: int,
) -> list[str]:
    actions: list[str] = []
    if buy_df.empty and sell_df.empty and sell_exit_df.empty:
        return actions

    _refresh_holding_prices()

    now = ist_now()
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
        and ist_now().time() >= profit_guard_after
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
    if ist_now().time() >= SQUARE_OFF:
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
            min_expected_profit_per_trade=min_expected_profit_per_trade,
        )
        bias_map = _symbol_bias_map()
        if not _buy_df.empty:
            _buy_df = _buy_df.copy()
            _buy_df["symbol_bias"] = _buy_df["symbol"].map(
                lambda s: float(bias_map.get(str(s), 0.0))
            )
            _buy_df["effective_buy_score"] = (
                pd.to_numeric(_buy_df["buy_score"],
                              errors="coerce").fillna(0.0)
                + pd.to_numeric(_buy_df["symbol_bias"],
                                errors="coerce").fillna(0.0)
            ).round(2)
        if not _sell_df.empty:
            _sell_df = _sell_df.copy()
            _sell_df["symbol_bias"] = _sell_df["symbol"].map(
                lambda s: float(bias_map.get(str(s), 0.0))
            )
            _sell_df["effective_sell_score"] = (
                pd.to_numeric(_sell_df["sell_score"],
                              errors="coerce").fillna(0.0)
                + pd.to_numeric(_sell_df["symbol_bias"],
                                errors="coerce").fillna(0.0)
            ).round(2)
        return _buy_df, _sell_df

    while entries_today < int(max_trades_day) and open_positions < int(max_positions):
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
            qty = max(0, min(qty_by_risk, qty_by_cash, max_qty_limit))

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
    if enable_short_selling:
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
                qty = max(0, min(qty_by_risk, qty_by_margin, max_qty_limit))

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

saved_state = _read_saved_state()
saved_ui = saved_state.get("ui_config", {}) if isinstance(
    saved_state, dict) else {}

with st.sidebar:
    st.header("Setup")
    total_capital = float(st.number_input(
        "Total Capital (Rs)", min_value=1000.0, value=float(saved_ui.get("total_capital", saved_state.get("start", 200000.0))), step=1000.0))
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

    st.header("Signal Filter")
    min_price = float(st.number_input("Min Price (Rs)",
                      min_value=0.0, max_value=50000.0, value=float(saved_ui.get("min_price", 50.0)), step=10.0))
    max_price = float(st.number_input(
        "Max Price (Rs)", min_value=1.0, max_value=50000.0, value=float(saved_ui.get("max_price", 1800.0)), step=10.0))
    min_buy_score = float(st.slider(
        "Minimum Buy Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_buy_score", 40.0)), step=1.0))
    min_order_value = float(st.number_input(
        "Minimum Order Value (Rs)", min_value=100.0, max_value=5000000.0,
        value=float(saved_ui.get("min_order_value", 50000.0)), step=1000.0,
        help="Avoids tiny orders that get eaten by charges"))
    min_expected_profit_per_trade = float(st.number_input(
        "Minimum Expected Profit Per Trade (Rs)", min_value=100.0, max_value=500000.0,
        value=float(saved_ui.get("min_expected_profit_per_trade", 1000.0)), step=100.0,
        help="BUY entries only. Uses position value x TP% as expected profit filter"))
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
        "Daily Profit Target (Rs)", min_value=500.0, value=float(saved_ui.get("daily_profit_target", 4000.0)), step=100.0))

    st.header("Run")
    auto_trade_on = st.checkbox("Enable Auto Paper Trading", value=bool(
        saved_ui.get("auto_trade_on", True)))
    auto_refresh_on = st.checkbox("Auto Refresh", value=bool(
        saved_ui.get("auto_refresh_on", True)))
    refresh_seconds = int(st.number_input(
        "Refresh Seconds", min_value=5, max_value=120, value=int(saved_ui.get("refresh_seconds", 20)), step=5))
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
    "min_price": float(min_price),
    "max_price": float(max_price),
    "min_buy_score": float(min_buy_score),
    "min_order_value": float(min_order_value),
    "min_expected_profit_per_trade": float(min_expected_profit_per_trade),
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
    "enable_learning_agent": bool(enable_learning_agent),
    "auto_apply_learning_suggestions": bool(auto_apply_learning_suggestions),
    "enable_profit_guard": bool(enable_profit_guard),
    "profit_guard_drawdown_pct": float(profit_guard_drawdown_pct),
    "profit_guard_after_hhmm": int(profit_guard_after_hhmm),
    "block_new_entries_on_guard": bool(block_new_entries_on_guard),
    "daily_profit_target": float(daily_profit_target),
    "auto_trade_on": bool(auto_trade_on),
    "auto_refresh_on": bool(auto_refresh_on),
    "refresh_seconds": int(refresh_seconds),
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

feasible_expected_cap = _max_feasible_expected_profit(
    total_capital=total_capital,
    cash_now=float(st.session_state.get("s_cash", total_capital)),
    max_symbol_allocation_pct=max_symbol_allocation_pct,
    max_total_deployment_pct=max_total_deployment_pct,
    max_qty_per_trade=max_qty_per_trade,
    max_price=max_price,
    tp_pct=tp_pct,
)
if float(min_expected_profit_per_trade) > feasible_expected_cap + 1e-9:
    min_expected_profit_per_trade = float(feasible_expected_cap)
    current_ui_config["min_expected_profit_per_trade"] = float(
        min_expected_profit_per_trade)
    config_guard_messages.append(
        f"Capped min expected profit/trade to feasible max: Rs {float(min_expected_profit_per_trade):,.0f}"
    )

if config_guard_messages:
    st.session_state.s_ui_config = current_ui_config
    _save_state()

if reset_btn:
    if STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)
    st.session_state.clear()
    st.rerun()

buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
    WATCHLIST,
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
    min_expected_profit_per_trade=min_expected_profit_per_trade,
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
    base_min_expected_profit_per_trade=min_expected_profit_per_trade,
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
        old_min_expected = float(min_expected_profit_per_trade)

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
        suggested_min_expected_profit = float(
            agent_plan.get("suggested_min_expected_profit_per_trade",
                           min_expected_profit_per_trade)
        )
        feasible_expected_profit_cap = _max_feasible_expected_profit(
            total_capital=total_capital,
            cash_now=float(st.session_state.get("s_cash", total_capital)),
            max_symbol_allocation_pct=max_symbol_allocation_pct,
            max_total_deployment_pct=max_total_deployment_pct,
            max_qty_per_trade=max_qty_per_trade,
            max_price=max_price,
            tp_pct=tp_pct,
        )
        min_expected_profit_per_trade = float(min(
            suggested_min_expected_profit,
            feasible_expected_profit_cap,
        ))

        effective_min_buy_score = float(min_buy_score)
        effective_min_short_score = float(min_short_score)
        effective_tp_pct = float(tp_pct)

        ranking_filters_changed = (
            abs(float(tp_pct) - old_tp_pct) > 1e-9
            or abs(float(min_expected_profit_per_trade) - old_min_expected) > 1e-9
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
        if abs(float(min_expected_profit_per_trade) - float(current_ui_config.get("min_expected_profit_per_trade", min_expected_profit_per_trade))) > 1e-9:
            learning_apply_messages.append(
                f"Applied min expected profit/trade: Rs {float(min_expected_profit_per_trade):,.0f}"
            )
        if suggested_min_expected_profit > feasible_expected_profit_cap + 1e-9:
            learning_apply_messages.append(
                f"Capped min expected profit/trade to feasible max: Rs {float(feasible_expected_profit_cap):,.0f}"
            )

        current_ui_config["min_buy_score"] = float(min_buy_score)
        current_ui_config["min_short_score"] = float(min_short_score)
        current_ui_config["tp_pct_display"] = float(tp_pct * 100.0)
        current_ui_config["idle_buy_fallback_minutes"] = int(
            idle_buy_fallback_minutes)
        current_ui_config["reentry_cooldown_minutes"] = int(
            reentry_cooldown_minutes)
        current_ui_config["min_expected_profit_per_trade"] = float(
            min_expected_profit_per_trade)
        st.session_state.s_ui_config = current_ui_config
        _save_state()

# Keep ranking profit filters aligned with the final TP used by strategy.
if abs(float(effective_tp_pct) - float(tp_pct)) > 1e-9 or ranking_filters_changed:
    buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
        WATCHLIST,
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
        min_expected_profit_per_trade=min_expected_profit_per_trade,
    )

if not buy_df.empty:
    buy_df = buy_df.copy()
    buy_df["symbol_bias"] = buy_df["symbol"].map(
        lambda s: float(symbol_bias.get(str(s), 0.0))
    )
    buy_df["effective_buy_score"] = (
        pd.to_numeric(buy_df["buy_score"], errors="coerce").fillna(0.0)
        + pd.to_numeric(buy_df["symbol_bias"], errors="coerce").fillna(0.0)
    ).round(2)

if not sell_df.empty:
    sell_df = sell_df.copy()
    sell_df["symbol_bias"] = sell_df["symbol"].map(
        lambda s: float(symbol_bias.get(str(s), 0.0))
    )
    sell_df["effective_sell_score"] = (
        pd.to_numeric(sell_df["sell_score"], errors="coerce").fillna(0.0)
        + pd.to_numeric(sell_df["symbol_bias"], errors="coerce").fillna(0.0)
    ).round(2)

if not sell_exit_df.empty:
    sell_exit_df = sell_exit_df.copy()
    sell_exit_df["symbol_bias"] = sell_exit_df["symbol"].map(
        lambda s: float(symbol_bias.get(str(s), 0.0))
    )
    sell_exit_df["effective_sell_score"] = (
        pd.to_numeric(sell_exit_df["sell_score"], errors="coerce").fillna(0.0)
        + pd.to_numeric(sell_exit_df["symbol_bias"],
                        errors="coerce").fillna(0.0)
    ).round(2)

actions: list[str] = []
if auto_trade_on:
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
        max_qty_per_trade=max_qty_per_trade,
        symbols=WATCHLIST,
        min_price=min_price,
        max_price=max_price,
        max_symbol_allocation_pct=max_symbol_allocation_pct,
        max_total_deployment_pct=max_total_deployment_pct,
        min_order_value=min_order_value,
        min_expected_profit_per_trade=min_expected_profit_per_trade,
        idle_buy_fallback_minutes=idle_buy_fallback_minutes,
    )

    if actions:
        # Show latest Top-5 after any executed trade in this cycle.
        buy_df, sell_df, sell_exit_df, scan_errors = _rank_signals(
            WATCHLIST,
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
            min_expected_profit_per_trade=min_expected_profit_per_trade,
        )

        if not buy_df.empty:
            buy_df = buy_df.copy()
            buy_df["symbol_bias"] = buy_df["symbol"].map(
                lambda s: float(symbol_bias.get(str(s), 0.0))
            )
            buy_df["effective_buy_score"] = (
                pd.to_numeric(buy_df["buy_score"], errors="coerce").fillna(0.0)
                + pd.to_numeric(buy_df["symbol_bias"],
                                errors="coerce").fillna(0.0)
            ).round(2)

        if not sell_df.empty:
            sell_df = sell_df.copy()
            sell_df["symbol_bias"] = sell_df["symbol"].map(
                lambda s: float(symbol_bias.get(str(s), 0.0))
            )
            sell_df["effective_sell_score"] = (
                pd.to_numeric(sell_df["sell_score"],
                              errors="coerce").fillna(0.0)
                + pd.to_numeric(sell_df["symbol_bias"],
                                errors="coerce").fillna(0.0)
            ).round(2)

        if not sell_exit_df.empty:
            sell_exit_df = sell_exit_df.copy()
            sell_exit_df["symbol_bias"] = sell_exit_df["symbol"].map(
                lambda s: float(symbol_bias.get(str(s), 0.0))
            )
            sell_exit_df["effective_sell_score"] = (
                pd.to_numeric(sell_exit_df["sell_score"],
                              errors="coerce").fillna(0.0)
                + pd.to_numeric(sell_exit_df["symbol_bias"],
                                errors="coerce").fillna(0.0)
            ).round(2)

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

holdings_df, unrealized, invested_capital = _portfolio_view()
realized = float(st.session_state.s_realized)
charges = float(st.session_state.s_charges)
cash = float(st.session_state.s_cash)
equity = cash + invested_capital + unrealized

today = ist_now().strftime("%Y-%m-%d")
today_realized = sum(
    float(row.get("realized_delta", 0.0))
    for row in st.session_state.s_log
    if str(row.get("ts", "")).startswith(today)
)

# Yesterday's PnL from history CSV
_yesterday = (ist_now() - timedelta(days=1)).strftime("%Y-%m-%d")
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

daily_pnl = today_realized + unrealized

a, b, c, d, e, f = st.columns(6)
a.metric("Start Capital", f"Rs {st.session_state.s_start:,.2f}")
b.metric("Current Equity", f"Rs {equity:,.2f}",
         delta=f"Rs {(equity - st.session_state.s_start):,.2f}")
c.metric("Cash", f"Rs {cash:,.2f}")
d.metric("Open PnL", f"Rs {unrealized:,.2f}")
e.metric("Realized PnL", f"Rs {realized:,.2f}")
f.metric("Total Charges", f"Rs {charges:,.2f}",
         delta=f"Net Rs {(realized - charges):,.2f}", delta_color="inverse")

progress = (daily_pnl / daily_profit_target) * \
    100.0 if daily_profit_target > 0 else 0.0
_yest_str = f"  |  Yesterday: Rs {yesterday_net:,.2f}" if yesterday_net is not None else ""
st.progress(min(1.0, max(0.0, progress / 100.0)),
            text=f"Today: Rs {daily_pnl:,.2f} / Rs {daily_profit_target:,.2f} ({progress:.1f}%){_yest_str}")

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
                            STATE_FILE, WATCHLIST, use_historical_data=True, historical_days=60)
                        st.success(f"✅ {result.get('status', 'done')}")

                        # Display detailed training info
                        st.write(f"**Training Summary:**")
                        st.write(
                            f"- Historical data samples: {result.get('historical_data_samples', 0)}")
                        st.write(
                            f"- Personal trade samples: {result.get('personal_trade_samples', 0)}")
                        st.write(
                            f"- Total training samples: {result.get('total_training_samples', 0)}")
                        st.write(
                            f"- LR accuracy: {result.get('lr_accuracy', 0):.1%}")
                        st.write(
                            f"- RF accuracy: {result.get('rf_accuracy', 0):.1%}")
                        st.write(f"- Note: {result.get('training_note', '')}")
                    except Exception as e:
                        st.error(f"Training failed: {e}")

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

    if get_symbol_quality_score is not None and st.session_state.s_ui_config.get("enable_ml_scoring", False):
        st.subheader("Symbol ML Quality Scores")
        scores: dict[str, float] = {}
        try:
            for sym in WATCHLIST:
                scores[sym] = get_symbol_quality_score(sym, STATE_FILE)
        except Exception:
            pass

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
        f"Clean closed trades exported: {clean_closed_trade_count} -> {CLEAN_CLOSED_TRADES_FILE}")

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
