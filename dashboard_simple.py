"""
Simple Budget-Based Trading Simulator (Paper Trading)
Run with: streamlit run dashboard_simple.py --server.port 8507
"""

from __future__ import annotations

import json
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


def _read_saved_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
    learning_memory: dict[str, Any],
    market_research: dict[str, Any],
) -> dict[str, Any]:
    adj_buy = 0.0
    adj_short = 0.0
    tp_mult = 1.0
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
                notes.append(
                    "Yesterday weak: tightened entries and quicker profit booking.")
            elif net > 0 and win_rate >= 60.0:
                adj_buy -= 1.0
                adj_short -= 1.0
                tp_mult *= 1.05
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
        notes.append("Sideways regime: faster profit booking.")

    return {
        "effective_min_buy_score": float(min(100.0, max(0.0, base_min_buy_score + adj_buy))),
        "effective_min_short_score": float(min(100.0, max(0.0, base_min_short_score + adj_short))),
        "effective_tp_pct": float(min(0.06, max(0.003, base_tp_pct * tp_mult))),
        "notes": notes,
        "latest_learning": latest,
    }


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


def _in_entry_window() -> bool:
    now = ist_now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    return MARKET_OPEN <= t <= ENTRY_CUTOFF


def _rank_signals(symbols: list[str], min_price: float, max_price: float) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
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

    all_df = pd.DataFrame(rows)
    if all_df.empty:
        return pd.DataFrame(), pd.DataFrame(), errors

    buy_df = all_df.sort_values(["buy_score", "pchange"], ascending=[
                                False, False]).head(5).reset_index(drop=True)
    sell_df = all_df.sort_values(["sell_score", "pchange"], ascending=[
                                 False, True]).head(5).reset_index(drop=True)
    return buy_df, sell_df, errors


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
) -> list[str]:
    actions: list[str] = []
    if buy_df.empty and sell_df.empty:
        return actions

    _refresh_holding_prices()

    now = ist_now()
    today = now.strftime("%Y-%m-%d")

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
            actions.append(f"SELL {sym}: SL hit @ Rs {ltp:.2f}")
        elif ltp >= target_price:
            _record_trade(sym, "SELL", qty, ltp, "Auto TP")
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
            actions.append(f"COVER {sym}: short SL hit @ Rs {ltp:.2f}")
        elif ltp <= target_price:
            _record_trade(sym, "COVER", qty, ltp, "Auto short TP")
            actions.append(f"COVER {sym}: short TP hit @ Rs {ltp:.2f}")

    # Optional: exit holdings when sell signal is READY with sufficient score.
    signal_exits_done = 0
    if enable_signal_sell and (not sell_df.empty):
        for _, row in sell_df.iterrows():
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

    for _, row in buy_df.iterrows():
        if entries_today >= int(max_trades_day):
            break
        if open_positions >= int(max_positions):
            break

        sym = str(row["symbol"])
        if sym in st.session_state.s_holdings or sym in st.session_state.s_shorts:
            continue
        if str(row.get("buy_signal", "WAIT")) != "READY":
            continue

        price = float(row.get("price", 0.0) or 0.0)
        score = float(
            row.get("effective_buy_score", row.get("buy_score", 0.0)) or 0.0
        )
        if price <= 0 or score < float(min_buy_score):
            actions.append(
                f"BUY {sym} SKIPPED: score {score:.1f} < min {float(min_buy_score):.1f}")
            continue

        risk_per_share = max(price * float(sl_pct), 0.01)
        qty_by_risk = int(risk_budget // risk_per_share)
        qty_by_cash = int(st.session_state.s_cash // max(price, 1e-6))
        qty = max(0, min(qty_by_risk, qty_by_cash, 300))

        if qty <= 0:
            actions.append(f"BUY {sym} SKIPPED: insufficient cash/risk budget")
            continue

        est_value = qty * price
        est_ch = _intraday_charges("BUY", est_value)
        if st.session_state.s_cash < (est_value + est_ch):
            actions.append(f"BUY {sym} SKIPPED: insufficient cash")
            continue

        _record_trade(sym, "BUY", qty, price, "Auto BUY signal",
                      sl_pct=sl_pct, tp_pct=tp_pct)
        actions.append(f"BUY {sym} {qty}qty @ Rs {price:.2f}")
        entries_today += 1
        open_positions += 1

    # Optional intraday short entries from sell signals.
    if enable_short_selling and (not sell_df.empty):
        for _, row in sell_df.iterrows():
            if entries_today >= int(max_trades_day):
                break
            if open_positions >= int(max_positions):
                break

            sym = str(row.get("symbol", ""))
            if sym in st.session_state.s_holdings or sym in st.session_state.s_shorts:
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
            qty = max(0, min(qty_by_risk, qty_by_margin, 300))
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
            entries_today += 1
            open_positions += 1

    _refresh_holding_prices()
    return actions


def _portfolio_view() -> tuple[pd.DataFrame, float]:
    rows: list[dict[str, Any]] = []
    unreal = 0.0
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
    return pd.DataFrame(rows), float(unreal)


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
        "Max Trades Per Day", min_value=1, max_value=20, value=int(saved_ui.get("max_trades_day", 3)), step=1))
    max_open_positions = int(st.number_input(
        "Max Open Positions", min_value=1, max_value=10, value=int(saved_ui.get("max_open_positions", 3)), step=1))

    st.header("Signal Filter")
    min_price = float(st.number_input("Min Price (Rs)",
                      min_value=0.0, max_value=50000.0, value=float(saved_ui.get("min_price", 50.0)), step=10.0))
    max_price = float(st.number_input(
        "Max Price (Rs)", min_value=1.0, max_value=50000.0, value=float(saved_ui.get("max_price", 1800.0)), step=10.0))
    min_buy_score = float(st.slider(
        "Minimum Buy Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_buy_score", 40.0)), step=1.0))

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
    enable_short_selling = st.checkbox(
        "Enable intraday short selling", value=bool(saved_ui.get("enable_short_selling", True)))
    min_short_score = float(st.slider(
        "Minimum Short Score", min_value=0.0, max_value=100.0, value=float(saved_ui.get("min_short_score", 20.0)), step=1.0))

    st.header("Learning Agent")
    enable_learning_agent = st.checkbox(
        "Enable learning + market research", value=bool(saved_ui.get("enable_learning_agent", True))
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
    reset_btn = st.button("Reset Simulator", use_container_width=True)

_init_state(total_capital)

current_ui_config = {
    "total_capital": float(total_capital),
    "risk_pct": float(risk_pct),
    "max_trades_day": int(max_trades_day),
    "max_open_positions": int(max_open_positions),
    "min_price": float(min_price),
    "max_price": float(max_price),
    "min_buy_score": float(min_buy_score),
    "sl_pct_display": float(sl_pct * 100.0),
    "tp_pct_display": float(tp_pct * 100.0),
    "enable_signal_sell": bool(enable_signal_sell),
    "min_sell_score": float(min_sell_score),
    "max_signal_exits_per_cycle": int(max_signal_exits_per_cycle),
    "enable_short_selling": bool(enable_short_selling),
    "min_short_score": float(min_short_score),
    "enable_learning_agent": bool(enable_learning_agent),
    "enable_profit_guard": bool(enable_profit_guard),
    "profit_guard_drawdown_pct": float(profit_guard_drawdown_pct),
    "profit_guard_after_hhmm": int(profit_guard_after_hhmm),
    "block_new_entries_on_guard": bool(block_new_entries_on_guard),
    "daily_profit_target": float(daily_profit_target),
    "auto_trade_on": bool(auto_trade_on),
    "auto_refresh_on": bool(auto_refresh_on),
    "refresh_seconds": int(refresh_seconds),
}

if st.session_state.get("s_ui_config", {}) != current_ui_config:
    st.session_state.s_ui_config = current_ui_config
    _save_state()

if reset_btn:
    if STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)
    st.session_state.clear()
    st.rerun()

buy_df, sell_df, scan_errors = _rank_signals(
    WATCHLIST, min_price=min_price, max_price=max_price)

learning_memory = _update_learning_memory()
market_research = _market_research_from_signals(buy_df, sell_df)
symbol_bias = _symbol_bias_map()

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

agent_plan = _agent_tuning_plan(
    base_min_buy_score=min_buy_score,
    base_min_short_score=min_short_score,
    base_tp_pct=tp_pct,
    learning_memory=learning_memory,
    market_research=market_research,
)

effective_min_buy_score = min_buy_score
effective_min_short_score = min_short_score
effective_tp_pct = tp_pct
if enable_learning_agent:
    effective_min_buy_score = float(agent_plan["effective_min_buy_score"])
    effective_min_short_score = float(agent_plan["effective_min_short_score"])
    effective_tp_pct = float(agent_plan["effective_tp_pct"])

actions: list[str] = []
if auto_trade_on:
    actions = _auto_paper_cycle(
        buy_df=buy_df,
        sell_df=sell_df,
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
    )

holdings_df, unrealized = _portfolio_view()
realized = float(st.session_state.s_realized)
charges = float(st.session_state.s_charges)
cash = float(st.session_state.s_cash)
equity = cash + unrealized

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

if actions:
    with st.expander("\U0001f916 Auto-Trade Actions", expanded=True):
        for act in actions:
            st.write(f"- {act}")

c1, c2 = st.columns(2)
with c1:
    st.subheader("\U0001f525 Top 5 Buy Signals")
    if buy_df.empty:
        st.info("No buy candidates in selected price band.")
    else:
        show_buy = buy_df[["symbol", "price", "buy_score", "effective_buy_score",
                           "buy_signal", "pchange", "updated"]].copy()
        show_buy["price"] = show_buy["price"].map(
            lambda x: f"Rs {float(x):.2f}")
        st.dataframe(show_buy, use_container_width=True, hide_index=True)

with c2:
    st.subheader("\U0001f9ca Top 5 Sell Signals")
    if sell_df.empty:
        st.info("No sell candidates in selected price band.")
    else:
        show_sell = sell_df[["symbol", "price", "sell_score", "effective_sell_score",
                             "sell_signal", "pchange", "updated"]].copy()
        show_sell["price"] = show_sell["price"].map(
            lambda x: f"Rs {float(x):.2f}")
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
