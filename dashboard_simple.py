"""
Simple Budget-Based Trading Simulator (Paper Trading)
Run with: streamlit run dashboard_simple.py --server.port 8507

From repo root, ``.streamlit/config.toml`` enables save-to-rerun (no server restart).
"""

from __future__ import annotations
import random as _rng_mod

import json
import logging
import math
import os
import html
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from typing import Any
from datetime import datetime, time, timedelta

import pandas as pd
import pytz
import streamlit as st

try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None

try:
    from stockmarket.finnhub_client import fetch_quote as _finnhub_fetch_quote
except Exception:
    _finnhub_fetch_quote = None  # type: ignore

from stockmarket.persistence.paper_repo import get_paper_repo
from stockmarket.state import (
    counters_to_session_state,
    paper_state_to_session_state,
    session_state_to_counters,
    session_state_to_paper_state,
)


IST = pytz.timezone("Asia/Kolkata")
US_EASTERN = pytz.timezone("America/New_York")

_DASHBOARD_DATA_CONFIG_PATH = Path("dashboard_simple_data_config.json")
_DASHBOARD_DATA_CONFIG_DEFAULTS = {
    "us_market_data_batch_size": 20,
}

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

_DASHBOARD_MARKET_EXTRAS: dict[str, dict[str, Any]] = {
    "NSE": {
        "label": "India NSE",
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

_app_settings_cache = None

# Setup logging for server-side console output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger("dashboard_simple")


def _app_log(level: str, message: str) -> None:
    """Log to server console (Streamlit session verbose UI removed)."""
    if level.upper() == "INFO":
        logger.info(message)
    elif level.upper() == "WARNING":
        logger.warning(message)
    elif level.upper() == "ERROR":
        logger.error(message)
    else:
        logger.debug(message)


_ACTIVITY_STEPS_MAX = 15


def _normalize_activity_steps() -> list[dict[str, str]]:
    """Ensure s_activity_steps is list[{ts, msg}]; coerce legacy string rows."""
    raw = st.session_state.get("s_activity_steps")
    if not isinstance(raw, list):
        st.session_state.s_activity_steps = []
        return []
    if len(raw) == 0:
        st.session_state.s_activity_steps = []
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and "msg" in item:
            out.append(
                {
                    "ts": str(item.get("ts", "—")),
                    "msg": str(item.get("msg", "")),
                }
            )
        elif isinstance(item, str):
            out.append({"ts": "—", "msg": item})
    st.session_state.s_activity_steps = out
    return out


def _activity_step(message: str) -> None:
    """Append one timestamped phase line for the Activity panel."""
    _normalize_activity_steps()
    ts = datetime.now(IST).strftime("%H:%M:%S")
    steps = st.session_state.s_activity_steps
    steps.append({"ts": ts, "msg": message})
    if len(steps) > _ACTIVITY_STEPS_MAX:
        del steps[: len(steps) - _ACTIVITY_STEPS_MAX]


def _activity_finish_summary(
    *,
    auto_refresh_on: bool,
    refresh_seconds: int,
    auto_trade_on: bool,
    scan_errors: int,
) -> None:
    bits: list[str] = []
    if auto_trade_on:
        bits.append("auto-trade on")
    if auto_refresh_on:
        bits.append(f"refresh every {int(refresh_seconds)}s")
    mode = ", ".join(bits) if bits else "manual refresh only"
    err = f", {scan_errors} scan error(s)" if scan_errors else ""
    _activity_step(f"Run summary: {mode}{err}")


def _render_activity_and_logs(
    *,
    auto_refresh_on: bool,
    refresh_seconds: int,
    auto_trade_on: bool,
) -> None:
    """Processing/idle header, resizable scrollable activity list, refresh."""
    steps = _normalize_activity_steps()
    live = bool(auto_refresh_on or auto_trade_on)
    title = "Processing" if live else "Idle"
    dot = "#198754" if live else "#dc3545"
    latest_preview = ""
    if steps:
        last = steps[-1]
        latest_preview = f"{last['ts']} — {last['msg']}"

    preview_html = ""
    if latest_preview:
        preview_html = (
            '<br/><span style="color:#495057;font-size:13px;">'
            + html.escape(latest_preview)
            + "</span>"
        )

    if steps:
        parts: list[str] = []
        for entry in steps:
            ts_e = html.escape(entry["ts"])
            msg_e = html.escape(entry["msg"])
            parts.append(
                f'<div style="line-height:1.35;"><strong>{ts_e}</strong> — {msg_e}</div>'
            )
        lines_html = "".join(parts)
    else:
        lines_html = (
            '<div style="line-height:1.35;color:#6c757d;">'
            "No activity steps recorded yet.</div>"
        )

    # ~15 lines tall by default; scrollbar inside; vertical resize; content does not stretch page.
    scroll_box = (
        '<div style="line-height:1.35;font-size:14px;color:#212529;margin-top:6px;">'
        '<div style="color:#6c757d;font-size:12px;margin-bottom:4px;">'
        "Activity (newest last)</div>"
        '<div style="'
        "overflow-y:auto;overflow-x:auto;resize:vertical;"
        "height:15lh;min-height:8rem;max-height:40rem;"
        "box-sizing:border-box;border:1px solid #dee2e6;border-radius:6px;"
        "padding:8px 10px;background:#fafafa;"
        '">'
        f"{lines_html}</div></div>"
    )

    with st.expander("Activity", expanded=False):
        hdr_l, hdr_r = st.columns([4, 1])
        with hdr_l:
            st.markdown(
                f'<p style="font-size:14px;line-height:1.35;margin:0;">'
                f'<span style="color:{dot};font-weight:700;">●</span>'
                f'<span style="font-weight:600;"> {title}</span>'
                f"{preview_html}"
                f"</p>",
                unsafe_allow_html=True,
            )
        with hdr_r:
            if st.button(
                "Refresh now",
                key="simple_manual_refresh",
                help="Full dashboard rerun (quotes, ranking, auto-trade).",
            ):
                st.rerun()

        st.html(scroll_box)


try:
    from stockmarket.quotes import get_default_quote_service
except Exception:
    get_default_quote_service = None  # type: ignore


_optimizer_mod: tuple[Any, Any, Any, Any] | None = None


def _optimizer_imports() -> tuple[Any, Any, Any, Any]:
    global _optimizer_mod
    if _optimizer_mod is None:
        try:
            from stockmarket.config import TradingConfig
            from stockmarket.optimizer import (
                collect_clean_closed_trades,
                export_optimization_report,
                run_intelligent_optimization,
            )

            _optimizer_mod = (
                TradingConfig,
                collect_clean_closed_trades,
                export_optimization_report,
                run_intelligent_optimization,
            )
        except Exception:
            _optimizer_mod = (None, None, None, None)
    return _optimizer_mod


def _dashboard_scorer(bias_map: dict[str, float] | None = None):
    from stockmarket.ml import build_scorer, with_bias_map

    base = build_scorer(_state_file())
    if bias_map:
        return with_bias_map(base, bias_map)
    return base


def _quote_service():
    if get_default_quote_service is None:
        raise RuntimeError("stockmarket.quotes unavailable")
    return get_default_quote_service()


def _load_dashboard_data_config() -> dict[str, Any]:
    """Load dashboard_simple-specific data settings with safe defaults."""
    cfg = dict(_DASHBOARD_DATA_CONFIG_DEFAULTS)
    if not _DASHBOARD_DATA_CONFIG_PATH.exists():
        return cfg
    try:
        data = json.loads(_DASHBOARD_DATA_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cfg
        cfg["us_market_data_batch_size"] = max(1, int(data.get("us_market_data_batch_size", cfg["us_market_data_batch_size"])))
    except Exception:
        return dict(_DASHBOARD_DATA_CONFIG_DEFAULTS)
    return cfg


def _us_market_data_batch_size() -> int:
    cfg = _load_dashboard_data_config()
    return max(1, int(cfg.get("us_market_data_batch_size", 20)))


def _to_simple_quote(qobj: Any) -> dict[str, float]:
    """Normalize a quote object or dict to dashboard_simple numeric payload."""
    if qobj is None:
        raise ValueError("missing quote")
    if hasattr(qobj, "to_simple_dict") and callable(qobj.to_simple_dict):
        raw = qobj.to_simple_dict()
        if isinstance(raw, dict):
            return {
                "symbol": str(raw.get("symbol", "")),
                "price": float(raw.get("price", 0.0) or 0.0),
                "vwap": float(raw.get("vwap", 0.0) or 0.0),
                "pchange": float(raw.get("pchange", 0.0) or 0.0),
                "range_pct": float(raw.get("range_pct", 0.0) or 0.0),
            }
    if isinstance(qobj, dict):
        return {
            "symbol": str(qobj.get("symbol", "")),
            "price": float(qobj.get("price", 0.0) or 0.0),
            "vwap": float(qobj.get("vwap", 0.0) or 0.0),
            "pchange": float(qobj.get("pchange", 0.0) or 0.0),
            "range_pct": float(qobj.get("range_pct", 0.0) or 0.0),
        }
    raise ValueError("invalid quote payload")


def _use_app_settings() -> bool:
    return os.environ.get("USE_APP_SETTINGS") == "1"


def _get_app_settings():
    global _app_settings_cache
    if _app_settings_cache is None:
        from stockmarket.settings import load_app_settings

        _app_settings_cache = load_app_settings()
    return _app_settings_cache


def _hhmm_to_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _profile_to_market_cfg(profile, extras: dict[str, Any]) -> dict[str, Any]:
    tz = IST if profile.timezone == "Asia/Kolkata" else US_EASTERN
    return {
        "label": extras["label"],
        "timezone": tz,
        "market_open": _hhmm_to_time(profile.market_open),
        "entry_cutoff": _hhmm_to_time(profile.entry_cutoff_time),
        "square_off": _hhmm_to_time(profile.square_off_time),
        "watchlist": list(profile.watchlist),
        "state_file": extras["state_file"],
        "clean_closed_trades_file": extras["clean_closed_trades_file"],
        "currency": extras["currency"],
        "score_change_weight": extras["score_change_weight"],
        "score_vwap_weight": extras["score_vwap_weight"],
        "score_range_weight": extras["score_range_weight"],
        "ready_pchange_threshold": extras["ready_pchange_threshold"],
        "ready_range_threshold": extras["ready_range_threshold"],
    }


def _market_keys() -> tuple[str, ...]:
    return ("NSE", "US")


def _market_label(market: str) -> str:
    if _use_app_settings():
        return str(_DASHBOARD_MARKET_EXTRAS[market]["label"])
    return str(MARKET_CONFIG[market]["label"])


def _selected_market() -> str:
    market = str(st.session_state.get("selected_market", "NSE")).upper()
    return market if market in _market_keys() else "NSE"


def _market_cfg() -> dict[str, Any]:
    market = _selected_market()
    if _use_app_settings():
        profile = _get_app_settings().market[market]
        return _profile_to_market_cfg(profile, _DASHBOARD_MARKET_EXTRAS[market])
    return MARKET_CONFIG[market]


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


def _use_paper_repo() -> bool:
    return os.environ.get("USE_PAPER_REPO") == "1"


def _use_trading_cycle() -> bool:
    return os.environ.get("USE_TRADING_CYCLE") == "1"


def _use_simple_views() -> bool:
    return os.environ.get("USE_SIMPLE_VIEWS") == "1"


def _view_flag_enabled(name: str) -> bool:
    """Per-slice phase-8b flag with fallback to the master USE_SIMPLE_VIEWS flag."""
    explicit = os.environ.get(f"USE_SIMPLE_VIEWS_{name}")
    if explicit is not None:
        return explicit == "1"
    return _use_simple_views()


def _paper_repo():
    return get_paper_repo(_state_file(), market=_selected_market())


def _auto_export_clean_closed_trades() -> tuple[int, str | None]:
    _, collect_clean_closed_trades, _, _ = _optimizer_imports()
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


def _optimizer_last_run_key() -> str:
    return f"s_optimizer_last_run_ts_{_selected_market()}"


def _optimizer_cfg_for_dashboard(TradingConfig: Any) -> Any:
    cfg_path = Path("config.json")
    cfg = TradingConfig.from_json(cfg_path) if cfg_path.exists() else TradingConfig()
    if _selected_market() == "US":
        cfg.market_timezone = "America/New_York"
    else:
        cfg.market_timezone = "Asia/Kolkata"
    return cfg


def _run_optimizer_from_dashboard(
    lookback_trades: int,
    min_train_trades: int,
    quality_threshold: float,
) -> tuple[dict[str, Any] | None, dict[str, str] | None, str | None]:
    (
        TradingConfig,
        _,
        export_optimization_report,
        run_intelligent_optimization,
    ) = _optimizer_imports()
    if (
        TradingConfig is None
        or run_intelligent_optimization is None
        or export_optimization_report is None
    ):
        return None, None, "Optimizer module unavailable in dashboard runtime."

    try:
        cfg = _optimizer_cfg_for_dashboard(TradingConfig)
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

    holdings_df, unrealized, positions_mtm = _portfolio_view()
    realized = float(st.session_state.s_realized)
    charges = float(st.session_state.s_charges)
    cash = float(st.session_state.s_cash)
    equity = cash + positions_mtm

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

    hdr, opt = st.columns([5, 1])
    with hdr:
        st.caption("Portfolio summary")
    with opt:
        table_view = st.checkbox(
            "Table view",
            value=bool(st.session_state.get("portfolio_metrics_tabular", False)),
            key="portfolio_metrics_tabular",
            help="Show the summary row as a compact table (full numbers, no truncation).",
        )

    equity_delta = equity - float(st.session_state.s_start)
    net_realized = realized - charges

    if table_view:
        summary_tbl = pd.DataFrame(
            [
                {
                    "Metric": "Start Capital",
                    "Amount": f"{currency_symbol} {float(st.session_state.s_start):,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Current Equity",
                    "Amount": f"{currency_symbol} {equity:,.2f}",
                    "Detail": f"Δ {currency_symbol} {equity_delta:,.2f}",
                },
                {
                    "Metric": "Cash",
                    "Amount": f"{currency_symbol} {cash:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Open PnL",
                    "Amount": f"{currency_symbol} {unrealized:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Realized PnL",
                    "Amount": f"{currency_symbol} {realized:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Total Charges",
                    "Amount": f"{currency_symbol} {charges:,.2f}",
                    "Detail": f"Net {currency_symbol} {net_realized:,.2f}",
                },
            ]
        )
        st.dataframe(
            summary_tbl,
            width="stretch",
            hide_index=True,
            column_config={
                "Metric": st.column_config.TextColumn("Metric", width="small"),
                "Amount": st.column_config.TextColumn("Amount", width="medium"),
                "Detail": st.column_config.TextColumn("Detail", width="medium"),
            },
        )
    else:
        # ~14–15px text so 6-up layout does not truncate like default st.metric
        lab = "font-size:13px;color:#6c757d;margin:0;line-height:1.2;"
        val = "font-size:15px;font-weight:600;margin:0;line-height:1.25;white-space:normal;word-break:break-word;"
        sub = "font-size:12px;margin:0;line-height:1.2;color:#198754;"
        sub_inv = "font-size:12px;margin:0;line-height:1.2;color:#dc3545;"
        a, b, c, d, e, f = st.columns(6)
        with a:
            st.markdown(
                f'<p style="{lab}">Start Capital</p>'
                f'<p style="{val}">{currency_symbol} {float(st.session_state.s_start):,.2f}</p>',
                unsafe_allow_html=True,
            )
        with b:
            ed_col = sub if equity_delta >= 0 else sub_inv
            st.markdown(
                f'<p style="{lab}">Current Equity</p>'
                f'<p style="{val}">{currency_symbol} {equity:,.2f}</p>'
                f'<p style="{ed_col}">Δ {currency_symbol} {equity_delta:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with c:
            st.markdown(
                f'<p style="{lab}">Cash</p>'
                f'<p style="{val}">{currency_symbol} {cash:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with d:
            st.markdown(
                f'<p style="{lab}">Open PnL</p>'
                f'<p style="{val}">{currency_symbol} {unrealized:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with e:
            st.markdown(
                f'<p style="{lab}">Realized PnL</p>'
                f'<p style="{val}">{currency_symbol} {realized:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with f:
            net_style = sub if net_realized >= 0 else sub_inv
            st.markdown(
                f'<p style="{lab}">Total Charges</p>'
                f'<p style="{val}">{currency_symbol} {charges:,.2f}</p>'
                f'<p style="{net_style}">Net {currency_symbol} {net_realized:,.2f}</p>',
                unsafe_allow_html=True,
            )

    progress = (daily_pnl / daily_profit_target) * \
        100.0 if daily_profit_target > 0 else 0.0
    st.progress(min(1.0, max(0.0, progress / 100.0)),
                text=f"Today: {currency_symbol} {daily_pnl:,.2f} / {currency_symbol} {daily_profit_target:,.2f} ({progress:.1f}%)")
    st.caption(
        "Equity = Cash + Σ(long: last price × qty) + short unrealized PnL. "
        "Short-sale proceeds are already in Cash. "
        "Δ vs start is not equal to Open PnL + net realized unless you have no open longs and no capital adjustments. "
        f"Net (under Total Charges) = Realized PnL − charges ({currency_symbol} {net_realized:,.2f})."
    )


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
        quote = _quote_service().get_nse_quote(symbol)
        result = quote.to_simple_dict()
        _app_log(
            "info",
            f"NSE {nse_symbol}: Rs {result['price']:.2f} ({result['pchange']:+.2f}%)",
        )
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
    return _fetch_us_quote_finnhub(symbol)


def _fetch_us_quote_finnhub(symbol: str) -> dict[str, float]:
    raw = _fetch_finnhub_quote_raw(symbol)
    _app_log("info", f"Fetching US quote via finnhub: {symbol}")
    if raw is None:
        raise ValueError(
            "Unable to fetch US quote from Finnhub (check FINNHUB_API_KEY and network)."
        )
    return _finnhub_payload_to_simple_dict(symbol, raw)


@st.cache_data(ttl=8, show_spinner=False)
def _fetch_finnhub_quote_raw(symbol: str, max_retries: int = 3) -> dict[str, Any] | None:
    """Fetch raw Finnhub quote with retry for transient failures."""
    if _finnhub_fetch_quote is None:
        _app_log("error", "stockmarket.finnhub_client unavailable for Finnhub quote fetch")
        return None
    import time as _time_mod
    for attempt in range(max_retries):
        try:
            return _finnhub_fetch_quote(symbol)
        except ValueError as exc:
            _app_log("error", str(exc))
            return None
        except Exception as exc:
            if attempt < max_retries - 1:
                _time_mod.sleep(0.5 * (2 ** attempt))
                continue
            _app_log("warning", f"Finnhub quote fetch failed for {symbol}: {exc}")
            return None
    return None


def _finnhub_payload_to_simple_dict(symbol: str, payload: dict[str, Any]) -> dict[str, float]:
    price = float(payload.get("c") or 0.0)
    high = float(payload.get("h") or price)
    low = float(payload.get("l") or price)
    open_price = float(payload.get("o") or price)
    prev_close = float(payload.get("pc") or price)
    vwap = (high + low + open_price + price) / 4.0 if price > 0 else 0.0
    pchange = ((price - prev_close) / max(prev_close, 1e-6)) * 100.0 if prev_close > 0 else 0.0
    range_pct = ((high - low) / max(price, 1e-6)) * 100.0 if price > 0 else 0.0
    return {
        "symbol": str(symbol).upper(),
        "price": float(price),
        "vwap": float(vwap),
        "pchange": float(pchange),
        "range_pct": float(range_pct),
    }


def _fetch_us_quotes_finnhub(symbols: list[str], batch_size: int = 20) -> dict[str, dict[str, float]]:
    # Finnhub has no batch quote endpoint; emulate batching with chunked parallel requests.
    if _finnhub_fetch_quote is None or not symbols:
        return {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: dict[str, dict[str, float]] = {}
    workers = max(1, min(12, max(1, int(batch_size))))
    symbols_list = list(symbols)
    chunks = [
        symbols_list[idx: idx + workers]
        for idx in range(0, len(symbols_list), workers)
    ]
    for chunk in chunks:
        if not chunk:
            continue
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            future_map = {pool.submit(_fetch_finnhub_quote_raw, sym): sym for sym in chunk}
            for fut in as_completed(future_map):
                sym = future_map[fut]
                try:
                    raw = fut.result()
                except Exception:
                    raw = None
                if isinstance(raw, dict):
                    out[sym] = _finnhub_payload_to_simple_dict(sym, raw)
    return out


def _fetch_us_quotes(symbols: list[str]) -> dict[str, Any]:
    """US bulk quotes via Finnhub (parallel requests)."""
    if _selected_market() != "US":
        return {}
    batch_size = _us_market_data_batch_size()
    return _fetch_us_quotes_finnhub(list(symbols), batch_size=batch_size)


def fetch_market_quote(symbol: str) -> dict[str, float]:
    if _selected_market() == "US":
        return fetch_us_quote(symbol)
    return fetch_nse_quote(symbol)


try:
    from stockmarket.simple_signals import scan_row_from_quote
except Exception:
    scan_row_from_quote = None  # type: ignore

try:
    from stockmarket.charges import intraday_charges_nse
except Exception:
    intraday_charges_nse = None  # type: ignore


def _intraday_charges(side: str, turnover: float) -> float:
    if intraday_charges_nse is None:
        brokerage = min(turnover * 0.0003, 20.0)
        exchange_txn = turnover * 0.0000325
        sebi = turnover * 0.000001
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        stamp = turnover * 0.00003 if side == "BUY" else 0.0
        stt = turnover * 0.00025 if side == "SELL" else 0.0
        return brokerage + exchange_txn + sebi + gst + stamp + stt
    return intraday_charges_nse(side, turnover)


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
    ]:
        st.session_state.pop(key, None)

    if _use_paper_repo():
        try:
            loaded = _paper_repo().load()
            if loaded:
                state, counters = loaded
                paper_state_to_session_state(st.session_state, state)
                counters_to_session_state(st.session_state, counters)
                st.session_state.s_state_file = state_key
                _app_log("info", f"Loaded state for {_selected_market()} market")
                return
        except Exception as e:
            _app_log("error", f"Failed to load saved state: {e}")
    elif state_file.exists():
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
    st.session_state.s_state_file = state_key
    _app_log(
        "info",
        f"Initialized fresh state for {_selected_market()} market with {_currency_symbol()} {starting_capital:,.0f}",
    )



def _save_state() -> None:
    if _use_paper_repo():
        state = session_state_to_paper_state(st.session_state)
        counters = session_state_to_counters(st.session_state)
        _paper_repo().save(state, counters)
        return

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


def _completed_trades_from_log(log_rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Return completed long round-trips by FIFO matching BUY -> SELL rows."""
    if not log_rows:
        return pd.DataFrame()

    rows = [row for row in log_rows if isinstance(row, dict)]
    if not rows:
        return pd.DataFrame()

    open_buys: dict[str, list[dict[str, Any]]] = {}
    completed: list[dict[str, Any]] = []

    ordered = sorted(rows, key=lambda r: str(r.get("ts", "")))

    for row in ordered:
        side = str(row.get("side", "")).upper()
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue

        qty = int(float(row.get("qty", 0.0) or 0))
        if qty <= 0:
            continue

        price = float(row.get("price", 0.0) or 0.0)
        total_charges = float(row.get("charges", 0.0) or 0.0)
        ts = str(row.get("ts", ""))
        cash_after = float(row.get("cash_after", 0.0) or 0.0)

        if side == "BUY":
            open_buys.setdefault(symbol, []).append(
                {
                    "qty": qty,
                    "price": price,
                    "charges": total_charges,
                    "ts": ts,
                }
            )
            continue

        if side != "SELL":
            continue

        open_lots = open_buys.get(symbol)
        if not open_lots:
            continue

        close_qty = qty
        while close_qty > 0 and open_lots:
            buy_leg = open_lots[0]
            open_qty = int(buy_leg.get("qty", 0) or 0)
            if open_qty <= 0:
                open_lots.pop(0)
                continue

            matched_qty = min(close_qty, open_qty)
            sell_qty = qty
            if sell_qty <= 0:
                matched_qty = 0

            if matched_qty <= 0:
                break

            buy_qty = float(buy_leg.get("qty", 0.0) or 0.0)
            buy_leg_charges = float(buy_leg.get("charges", 0.0) or 0.0)
            buy_ratio = matched_qty / buy_qty if buy_qty > 0 else 0.0
            sell_ratio = matched_qty / sell_qty if sell_qty > 0 else 0.0

            matched_buy_charges = buy_leg_charges * buy_ratio
            matched_sell_charges = total_charges * sell_ratio
            pair_charges = matched_buy_charges + matched_sell_charges
            pair_qty = float(matched_qty)
            buy_price = float(buy_leg.get("price", 0.0) or 0.0)
            sell_price = float(price)
            pnl = (sell_price - buy_price) * pair_qty - pair_charges

            completed.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "side": "LONG",
                    "quantity": matched_qty,
                    "buying_price": buy_price,
                    "selling_price": sell_price,
                    "charges": pair_charges,
                    "total_invested": (pair_qty * buy_price) + matched_buy_charges,
                    "total_collected": (pair_qty * sell_price) - matched_sell_charges,
                    "realized_pnl": pnl,
                    "reason": str(row.get("reason", "")).strip().splitlines()[0],
                    "cash_in_hand": cash_after,
                }
            )

            close_qty -= matched_qty
            buy_leg["qty"] = open_qty - matched_qty
            if buy_leg["qty"] <= 0:
                open_lots.pop(0)

    if not completed:
        return pd.DataFrame()

    return pd.DataFrame(completed)


def _build_raw_order_log(log_rows: list[dict[str, Any]], currency_symbol: str) -> pd.DataFrame:
    """Normalize raw trade legs with open/closed status and amounts."""
    if not log_rows:
        return pd.DataFrame()
    rows = [row for row in log_rows if isinstance(row, dict)]
    if not rows:
        return pd.DataFrame()

    ordered = sorted(rows, key=lambda r: str(r.get("ts", "")))
    positions: dict[str, float] = {}
    out: list[dict[str, Any]] = []

    for row in ordered:
        side = str(row.get("side", "")).upper()
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        qty = int(float(row.get("qty", 0.0) or 0))
        if qty <= 0:
            continue
        price = float(row.get("price", 0.0) or 0.0)
        ts = str(row.get("ts", ""))
        reason = str(row.get("reason", "")).strip()
        realized_delta = float(row.get("realized_delta", 0.0) or 0.0)

        delta = 0
        buy_price = None
        buy_amount = None
        sell_price = None
        sell_amount = None

        if side == "BUY":
            delta = qty
            buy_price = price
            buy_amount = float(qty) * price
        elif side == "SELL":
            delta = -qty
            sell_price = price
            sell_amount = float(qty) * price
        elif side == "SHORT":
            delta = -qty
            sell_price = price
            sell_amount = float(qty) * price
        elif side == "COVER":
            delta = qty
            buy_price = price
            buy_amount = float(qty) * price
        else:
            continue

        prev_pos = float(positions.get(symbol, 0.0))
        new_pos = prev_pos + float(delta)
        positions[symbol] = new_pos
        status = "Closed" if abs(new_pos) < 1e-9 else "Open"

        out.append(
            {
                "Timestamp": ts,
                "Symbol": symbol,
                "Side": side,
                "Qty": qty,
                "Buy price": buy_price,
                "Buy amount": buy_amount,
                "Sell price": sell_price,
                "Sell amount": sell_amount,
                "Profit/Loss amount": realized_delta,
                "Reason": reason,
                "Status": status,
            }
        )

    if not out:
        return pd.DataFrame()

    df = pd.DataFrame(out)
    df = df.sort_values("Timestamp", ascending=False).reset_index(drop=True)
    return df


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

    sym_list = list(symbols)
    quotes_map: dict[str, Any] = {}
    try:
        if _selected_market() == "US":
            quotes_map = _fetch_us_quotes(sym_list)
        else:
            svc = _quote_service()
            quotes_map = svc.get_nse_quotes(sym_list)
    except Exception as e:
        errors.append(f"batch quotes: {e}")

    for sym in symbols:
        try:
            qobj = quotes_map.get(sym)
            if qobj is None:
                raise ValueError("missing quote after batch fetch")
            q = _to_simple_quote(qobj)
            price = float(q["price"])
            if price <= 0:
                continue
            if price < float(min_price) or price > float(max_price):
                continue

            vwap = float(q["vwap"])
            pchange = float(q["pchange"])
            range_pct = float(q["range_pct"])

            if scan_row_from_quote is not None:
                rows.append(
                    scan_row_from_quote(
                        sym,
                        price,
                        vwap,
                        pchange,
                        range_pct,
                        market_now().strftime("%H:%M:%S"),
                        score_cfg,
                    )
                )
            else:
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
    return _dashboard_scorer().batch_scores(symbols)


def _apply_effective_scores(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    bias_map: dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    from stockmarket.cycle.scoring import apply_effective_scores

    ml_enabled = bool(
        st.session_state.get("s_ui_config", {}).get("enable_ml_scoring", False)
    )
    scorer = _dashboard_scorer(bias_map)
    state_file = _state_file()
    model_path = scorer.model_path() if hasattr(scorer, "model_path") else None
    model_file = (
        model_path
        if isinstance(model_path, Path)
        else Path("outputs") / "market_learning_model.pkl"
    )
    state_mtime = float(state_file.stat().st_mtime) if state_file.exists() else 0.0
    model_mtime = float(model_file.stat().st_mtime) if model_file.exists() else 0.0
    return apply_effective_scores(
        buy_df,
        sell_df,
        sell_exit_df,
        scorer,
        ml_enabled=ml_enabled,
        batch_ml_scores=_batch_ml_scores_cached,
        state_mtime=state_mtime,
        model_mtime=model_mtime,
    )


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

    _activity_step("Fetching watchlist quotes & ranking")
    with st.spinner("Fetching watchlist quotes…"):
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
    symbols = list(
        set(st.session_state.s_holdings.keys()) | set(st.session_state.s_shorts.keys())
    )
    if not symbols:
        return
    quotes_map: dict[str, Any] = {}
    try:
        if _selected_market() == "US":
            quotes_map = _fetch_us_quotes(symbols)
        else:
            svc = _quote_service()
            quotes_map = svc.get_nse_quotes(symbols)
    except Exception:
        quotes_map = {}

    for sym in symbols:
        qobj = quotes_map.get(sym)
        if qobj is not None:
            try:
                p = float(_to_simple_quote(qobj).get("price") or 0.0)
                if p > 0:
                    st.session_state.s_prices[sym] = p
                continue
            except Exception:
                pass
        try:
            q = fetch_market_quote(sym)
            p = float(q.get("price") or 0.0)
            if p > 0:
                st.session_state.s_prices[sym] = p
        except Exception:
            continue


def _build_cycle_settings(
    *,
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
    max_trade_invest_pct: float,
):
    from stockmarket.domain import (
        CycleSettings,
        GuardSettings,
        RiskSettings,
        SignalSettings,
    )

    return CycleSettings(
        risk=RiskSettings(
            risk_pct=float(risk_pct),
            sl_pct=float(sl_pct),
            tp_pct=float(tp_pct),
            max_trades_day=int(max_trades_day),
            max_positions=int(max_positions),
            max_qty_per_trade=int(max_qty_per_trade),
            max_symbol_allocation_pct=float(max_symbol_allocation_pct),
            max_total_deployment_pct=float(max_total_deployment_pct),
            max_trade_invest_pct=float(max_trade_invest_pct),
            min_order_value=float(min_order_value),
            min_price=float(min_price),
            max_price=float(max_price),
        ),
        signals=SignalSettings(
            min_buy_score=float(min_buy_score),
            min_sell_score=float(min_sell_score),
            min_short_score=float(min_short_score),
            enable_signal_sell=bool(enable_signal_sell),
            enable_short_selling=bool(enable_short_selling),
            max_signal_exits_per_cycle=int(max_signal_exits_per_cycle),
            idle_buy_fallback_minutes=int(idle_buy_fallback_minutes),
        ),
        guards=GuardSettings(
            enable_profit_guard=bool(enable_profit_guard),
            profit_guard_drawdown_pct=float(profit_guard_drawdown_pct),
            profit_guard_after=profit_guard_after,
            block_new_entries_on_guard=bool(block_new_entries_on_guard),
            daily_profit_target=float(daily_profit_target),
            reentry_cooldown_minutes=int(reentry_cooldown_minutes),
            reentry_min_move_pct=float(reentry_min_move_pct),
            sl_cooldown_after_stop_minutes=int(sl_cooldown_after_stop_minutes),
            enable_regime_entry_gate=bool(enable_regime_entry_gate),
            market_regime=str(market_regime or "unknown"),
        ),
        symbols=tuple(symbols),
    )


def _rank_signals_for_cycle(**kwargs):
    buy_df, sell_df, sell_exit_df, errors = _rank_signals(**kwargs)
    buy_df, sell_df, _ = _apply_effective_scores(
        buy_df,
        sell_df,
        pd.DataFrame(),
        _symbol_bias_map(),
    )
    return buy_df, sell_df, sell_exit_df, errors


def _build_cycle_services():
    from stockmarket.cycle.factory import build_services

    return build_services(
        session=st.session_state,
        market_now_fn=market_now,
        market_open=_market_open_time(),
        entry_cutoff=_entry_cutoff_time(),
        square_off=_square_off_time(),
        charges_fn=_intraday_charges,
        rank_signals_fn=_rank_signals_for_cycle,
        refresh_prices_fn=_refresh_holding_prices,
        persist_state_fn=_save_state,
        use_paper_repo=_use_paper_repo(),
        state_file=_state_file(),
        market=_selected_market(),
        scorer=_dashboard_scorer(_symbol_bias_map()),
    )


def _run_trading_cycle(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    sell_exit_df: pd.DataFrame,
    settings,
) -> list[str]:
    from stockmarket.cycle import CycleContext, run_cycle
    from stockmarket.cycle.ports import RankedSignals

    now = market_now()
    state = session_state_to_paper_state(st.session_state)
    counters = session_state_to_counters(st.session_state)
    ctx = CycleContext(
        now=now,
        today=now.strftime("%Y-%m-%d"),
        settings=settings,
        state=state,
        counters=counters,
        signals=RankedSignals(
            buy_df=buy_df,
            sell_df=sell_df,
            sell_exit_df=sell_exit_df,
        ),
    )
    ctx = run_cycle(ctx, _build_cycle_services())
    paper_state_to_session_state(st.session_state, ctx.state)
    counters_to_session_state(st.session_state, ctx.counters)
    return list(ctx.actions)


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
    if _use_trading_cycle():
        settings = _build_cycle_settings(
            risk_pct=risk_pct,
            max_trades_day=max_trades_day,
            max_positions=max_positions,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            min_buy_score=min_buy_score,
            enable_signal_sell=enable_signal_sell,
            min_sell_score=min_sell_score,
            max_signal_exits_per_cycle=max_signal_exits_per_cycle,
            enable_short_selling=enable_short_selling,
            min_short_score=min_short_score,
            enable_profit_guard=enable_profit_guard,
            profit_guard_drawdown_pct=profit_guard_drawdown_pct,
            profit_guard_after=profit_guard_after,
            block_new_entries_on_guard=block_new_entries_on_guard,
            daily_profit_target=daily_profit_target,
            reentry_cooldown_minutes=reentry_cooldown_minutes,
            reentry_min_move_pct=reentry_min_move_pct,
            enable_regime_entry_gate=enable_regime_entry_gate,
            market_regime=market_regime,
            sl_cooldown_after_stop_minutes=sl_cooldown_after_stop_minutes,
            max_qty_per_trade=max_qty_per_trade,
            symbols=symbols,
            min_price=min_price,
            max_price=max_price,
            max_symbol_allocation_pct=max_symbol_allocation_pct,
            max_total_deployment_pct=max_total_deployment_pct,
            min_order_value=min_order_value,
            idle_buy_fallback_minutes=idle_buy_fallback_minutes,
            max_trade_invest_pct=max_trade_invest_pct,
        )
        return _run_trading_cycle(buy_df, sell_df, sell_exit_df, settings)

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


def _portfolio_view() -> tuple[pd.DataFrame, float, float]:
    """Build holdings table and PnL figures.

    Third return value is the **mark-to-market add-on for equity** (excludes cash):
    sum(long: ltp*qty) + sum(short: (avg-ltp)*qty).

    Short sale proceeds are already included in ``s_cash``; we must not add
    ``avg*qty`` again for shorts (that would double-count and inflate equity).
    """
    rows: list[dict[str, Any]] = []
    unreal = 0.0
    long_market_value = 0.0
    short_unrealized = 0.0
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
        long_market_value += ltp * qty
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
        short_unrealized += pnl
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
    equity_mtm_addon = long_market_value + short_unrealized
    return pd.DataFrame(rows), float(unreal), float(equity_mtm_addon)


@st.fragment
def _fragment_live_tables_and_errors(
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    scan_errors: list[str],
) -> None:
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
            st.dataframe(
                show_buy,
                width='stretch',
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("Symbol", width="small"),
                    "price": st.column_config.TextColumn("Price", width="small"),
                    "rank_qty": st.column_config.NumberColumn("Qty", format="%d", width="small"),
                    "rank_order_value": st.column_config.TextColumn("Order value", width="medium"),
                    "rank_expected_profit": st.column_config.TextColumn("Expected profit", width="medium"),
                    "rank_symbol_headroom": st.column_config.TextColumn("Symbol headroom", width="medium"),
                    "rank_deployment_headroom": st.column_config.TextColumn("Deployment headroom", width="medium"),
                    "buy_score": st.column_config.NumberColumn("Score", format="%.1f", width="small"),
                    "research_buy_score": st.column_config.NumberColumn("Research", format="%.1f", width="small"),
                    "research_tag": st.column_config.TextColumn("Tag", width="medium"),
                    "effective_buy_score": st.column_config.NumberColumn("Effective", format="%.1f", width="small"),
                    "buy_signal": st.column_config.TextColumn("Signal", width="small"),
                    "pchange": st.column_config.NumberColumn("%Chg", format="%.2f", width="small"),
                    "updated": st.column_config.TextColumn("Updated", width="medium"),
                },
            )

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
            st.dataframe(
                show_sell,
                width='stretch',
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("Symbol", width="small"),
                    "price": st.column_config.TextColumn("Price", width="small"),
                    "rank_qty": st.column_config.NumberColumn("Qty", format="%d", width="small"),
                    "rank_order_value": st.column_config.TextColumn("Order value", width="medium"),
                    "rank_expected_profit": st.column_config.TextColumn("Expected profit", width="medium"),
                    "rank_symbol_headroom": st.column_config.TextColumn("Symbol headroom", width="medium"),
                    "rank_deployment_headroom": st.column_config.TextColumn("Deployment headroom", width="medium"),
                    "sell_score": st.column_config.NumberColumn("Score", format="%.1f", width="small"),
                    "research_sell_score": st.column_config.NumberColumn("Research", format="%.1f", width="small"),
                    "research_tag": st.column_config.TextColumn("Tag", width="medium"),
                    "effective_sell_score": st.column_config.NumberColumn("Effective", format="%.1f", width="small"),
                    "sell_signal": st.column_config.TextColumn("Signal", width="small"),
                    "pchange": st.column_config.NumberColumn("%Chg", format="%.2f", width="small"),
                    "updated": st.column_config.TextColumn("Updated", width="medium"),
                },
            )

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
        st.dataframe(
            view_h,
            width='stretch',
            hide_index=True,
            column_config={
                "side": st.column_config.TextColumn("Side", width="small"),
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "qty": st.column_config.NumberColumn("Qty", format="%d", width="small"),
                "avg": st.column_config.TextColumn("Avg", width="small"),
                "ltp": st.column_config.TextColumn("LTP", width="small"),
                "stop": st.column_config.TextColumn("Stop", width="small"),
                "target": st.column_config.TextColumn("Target", width="small"),
                "invested": st.column_config.TextColumn("Invested", width="medium"),
                "pnl": st.column_config.TextColumn("PnL", width="medium"),
                "pnl_pct": st.column_config.TextColumn("PnL %", width="small"),
            },
        )

    st.subheader("\U0001f4d2 Trade History")
    log_df = pd.DataFrame(st.session_state.s_log)
    completed_trades = _completed_trades_from_log(st.session_state.s_log)

    if completed_trades.empty:
        st.info("No completed trades yet")
    else:
        _trade_col_map: list[tuple[str, str]] = [
            ("timestamp", "Timestamp"),
            ("symbol", "Symbol"),
            ("side", "Side"),
            ("quantity", "Quantity"),
            ("buying_price", "Buying price"),
            ("selling_price", "Selling price"),
            ("charges", "Charges"),
            ("total_invested", "Total invested"),
            ("total_collected", "Total collected"),
            ("realized_pnl", "Realized P&L"),
            ("reason", "Reason"),
            ("cash_in_hand", "Cash after trade"),
        ]
        _sorted = completed_trades.sort_values("timestamp", ascending=False)
        _order_src = [src for src, _ in _trade_col_map if src in _sorted.columns]
        _rename = {src: dst for src, dst in _trade_col_map if src in _sorted.columns}
        show_history = _sorted[_order_src].rename(columns=_rename)

        def _rs_amount(v: object) -> float:
            if not isinstance(v, str):
                return 0.0
            s = v.replace("Rs ", "", 1).replace(",", "").strip()
            if not s:
                return 0.0
            return float(s)

        def _fmt_reason_cell(v: object) -> str:
            t = str(v).strip()
            if not t:
                return "\u2014"
            line = t.splitlines()[0].strip()
            if len(line) > 120:
                return line[:119] + "\u2026"
            return line

        _cur_cols = [
            "Buying price",
            "Selling price",
            "Charges",
            "Total invested",
            "Total collected",
            "Realized P&L",
            "Cash after trade",
        ]
        for _c in _cur_cols:
            if _c in show_history.columns:
                show_history[_c] = show_history[_c].map(lambda x: f"Rs {float(x):,.2f}")

        if "Quantity" in show_history.columns:
            show_history["Quantity"] = show_history["Quantity"].map(
                lambda x: int(float(x)))

        if "Reason" in show_history.columns:
            show_history["Reason"] = show_history["Reason"].map(_fmt_reason_cell)

        _pnl_label = "Realized P&L"

        def _highlight_trade_pnl(s: pd.Series) -> list[str]:
            return [
                "color: #16a34a" if _rs_amount(v) > 0 else
                "color: #dc2626" if _rs_amount(v) < 0 else
                ""
                for v in s
            ]

        st.dataframe(
            show_history.style.apply(_highlight_trade_pnl, subset=[_pnl_label]),
            width="stretch",
            hide_index=True,
            column_config={
                "Timestamp": st.column_config.TextColumn("Timestamp", width="medium"),
                "Symbol": st.column_config.TextColumn("Symbol", width="small"),
                "Side": st.column_config.TextColumn("Side", width="small"),
                "Quantity": st.column_config.NumberColumn("Quantity", format="%d", width="small"),
                "Buying price": st.column_config.TextColumn("Buying price", width="small"),
                "Selling price": st.column_config.TextColumn("Selling price", width="small"),
                "Charges": st.column_config.TextColumn("Charges", width="small"),
                "Total invested": st.column_config.TextColumn("Total invested", width="medium"),
                "Total collected": st.column_config.TextColumn("Total collected", width="medium"),
                _pnl_label: st.column_config.TextColumn(_pnl_label, width="medium"),
                "Reason": st.column_config.TextColumn("Reason", width="large"),
                "Cash after trade": st.column_config.TextColumn(
                    "Cash after trade", width="medium"
                ),
            },
        )

    if not log_df.empty:
        with st.expander("Raw order log (all legs)", expanded=False):
            view_log = _build_raw_order_log(st.session_state.s_log, _currency_symbol())
            if view_log.empty:
                st.info("No raw trade legs yet.")
            else:
                cur = _currency_symbol()
                def _fmt_money(val: object) -> str:
                    if val is None or (isinstance(val, float) and pd.isna(val)):
                        return "—"
                    return f"{cur} {float(val):,.2f}"

                money_cols = [
                    "Buy price",
                    "Buy amount",
                    "Sell price",
                    "Sell amount",
                    "Profit/Loss amount",
                ]
                for col in money_cols:
                    if col in view_log.columns:
                        view_log[col] = view_log[col].map(_fmt_money)
                if "Qty" in view_log.columns:
                    view_log["Qty"] = view_log["Qty"].map(lambda x: int(float(x)))

                st.dataframe(
                    view_log,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Timestamp": st.column_config.TextColumn("Timestamp", width="medium"),
                        "Symbol": st.column_config.TextColumn("Symbol", width="small"),
                        "Side": st.column_config.TextColumn("Side", width="small"),
                        "Qty": st.column_config.NumberColumn("Qty", format="%d", width="small"),
                        "Buy price": st.column_config.TextColumn("Buy price", width="small"),
                        "Buy amount": st.column_config.TextColumn("Buy amount", width="medium"),
                        "Sell price": st.column_config.TextColumn("Sell price", width="small"),
                        "Sell amount": st.column_config.TextColumn("Sell amount", width="medium"),
                        "Profit/Loss amount": st.column_config.TextColumn("Profit/Loss amount", width="medium"),
                        "Reason": st.column_config.TextColumn("Reason", width="large"),
                        "Status": st.column_config.TextColumn("Status", width="small"),
                    },
                )
    elif not completed_trades.empty:
        with st.expander("Raw order log (all legs)", expanded=False):
            st.info("No raw trade legs yet.")

    if scan_errors:
        with st.expander("Scan errors", expanded=False):
            st.text_area(
                "Scan error details",
                value="\n".join(scan_errors),
                height=min(420, 100 + 18 * len(scan_errors)),
                disabled=True,
                label_visibility="collapsed",
            )


def render_simple_dashboard(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Simple Budget Trading Simulator",
                           page_icon="\U0001f4b0", layout="wide")
    st.header("Simple Budget-Based Trading Simulator")
    
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
            format_func=lambda key: _market_label(key),
            key="selected_market",
            help="Each market keeps its own paper-trade state and ML model.",
        )
        if selected != st.session_state.get("_last_market"):
            st.session_state._last_market = selected
            st.session_state.s_skip_optimizer_after_market_switch = True
            _app_log("info", f"Switched to {_market_label(selected)} market")
    
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
            "Run Optimizer Now", width='stretch')
        topup_small_positions_btn = st.button(
            "Top Up Existing Tiny Positions", width='stretch')
        reset_btn = st.button("Reset Simulator", width='stretch')
    
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

    _init_state(total_capital)

    # Must run after _init_state: _save_state reads s_cash and other keys.
    if full_auto_paper_mode:
        ui_cfg = st.session_state.get("s_ui_config", {})
        if not isinstance(ui_cfg, dict):
            ui_cfg = {}
        if not bool(ui_cfg.get("enable_ml_scoring", False)):
            ui_cfg["enable_ml_scoring"] = True
            st.session_state.s_ui_config = ui_cfg
            _save_state()
    
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
    _activity_step(
        f"Scan universe: {len(active_watchlist)} symbols ({_selected_market()})"
    )

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
        _activity_step("Auto-trade cycle")
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
            _activity_step(f"Auto-trade done ({len(actions)} action(s))")
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

        else:
            _activity_step("Auto-trade done (no actions)")

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
        st.session_state.get(_optimizer_last_run_key(), 0.0))
    _now_ts = ist_now().timestamp()
    _skip_after_market_switch = bool(
        st.session_state.pop("s_skip_optimizer_after_market_switch", False)
    )
    _should_auto_run_optimizer = bool(optimizer_auto_run) and not _skip_after_market_switch and (
        optimizer_last_run_ts <= 0.0
        or (_now_ts - optimizer_last_run_ts) >= (float(optimizer_interval_minutes) * 60.0)
    )
    _should_run_optimizer = bool(
        run_optimizer_now_btn) or _should_auto_run_optimizer
    if _should_run_optimizer:
        _activity_step("Optimizer")
        with st.spinner("Running optimizer analysis..."):
            _summary, _artifacts, _optimizer_err = _run_optimizer_from_dashboard(
                lookback_trades=optimizer_lookback_trades,
                min_train_trades=optimizer_min_train_trades,
                quality_threshold=optimizer_quality_threshold,
            )
        if _optimizer_err is None:
            st.session_state.s_optimizer_summary = _summary
            st.session_state.s_optimizer_artifacts = _artifacts
            st.session_state[_optimizer_last_run_key()] = _now_ts
            optimizer_summary = _summary
            optimizer_artifacts = _artifacts
        else:
            st.session_state.s_optimizer_error = _optimizer_err
    
    _activity_finish_summary(
        auto_refresh_on=auto_refresh_on,
        refresh_seconds=refresh_seconds,
        auto_trade_on=auto_trade_on,
        scan_errors=len(scan_errors),
    )
    if _view_flag_enabled("ACTIVITY_LOGS"):
        from stockmarket.views.simple_activity_and_logs import (
            render_activity_and_logs,
        )

        render_activity_and_logs(
            steps=_normalize_activity_steps(),
            auto_refresh_on=auto_refresh_on,
            auto_trade_on=auto_trade_on,
            on_manual_refresh=st.rerun,
        )
    else:
        _render_activity_and_logs(
            auto_refresh_on=auto_refresh_on,
            refresh_seconds=refresh_seconds,
            auto_trade_on=auto_trade_on,
        )

    # Display portfolio metrics with auto price refresh every page load/refresh
    _quick_portfolio_metrics()
    
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
    
    if _view_flag_enabled("TOMORROW_PLAN"):
        from stockmarket.views.simple_tomorrow_plan import render_tomorrow_plan

        render_tomorrow_plan(
            agent_plan=agent_plan if isinstance(agent_plan, dict) else {},
            market_research=market_research if isinstance(market_research, dict) else {},
            learning_memory=learning_memory if isinstance(learning_memory, dict) else {},
            effective_min_buy_score=float(effective_min_buy_score),
            effective_min_short_score=float(effective_min_short_score),
            effective_tp_pct=float(effective_tp_pct),
            learning_apply_messages=list(learning_apply_messages or []),
            config_guard_messages=list(config_guard_messages or []),
        )
    else:
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
                ml_scorer = _dashboard_scorer()
                if not ml_scorer.can_train():
                    st.error(
                        "ML module not available; install: pip install scikit-learn"
                    )
                else:
                    with st.spinner("🔄 Fetching 60 days historical data + training model..."):
                        try:
                            result = ml_scorer.train(
                                active_watchlist, use_historical_data=True, historical_days=60)
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
    
        ml_scorer = _dashboard_scorer()

        if (
            ml_scorer.supports_ml_scoring()
            and st.session_state.s_ui_config.get("enable_ml_scoring", False)
            and show_ml_scores_table
        ):
            st.subheader("Symbol ML Quality Scores")
            state_file = _state_file()
            state_mtime = float(
                state_file.stat().st_mtime) if state_file.exists() else 0.0
            model_path = ml_scorer.model_path()
            model_file = (
                model_path
                if isinstance(model_path, Path)
                else Path("outputs") / "market_learning_model.pkl"
            )
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
                st.dataframe(score_df, width='stretch', hide_index=True)
    
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
    optimizer_error = st.session_state.get("s_optimizer_error")
    market_regime = str(market_research.get("regime", "unknown"))

    if _view_flag_enabled("TOP_PANELS"):
        from stockmarket.views.simple_top_panels import (
            render_ai_best_action,
            render_auto_trade_actions,
            render_clean_closed_trades_status,
            render_optimizer_summary,
        )

        render_auto_trade_actions(actions)
        render_clean_closed_trades_status(
            count=int(clean_closed_trade_count),
            error=clean_export_err,
            path=str(_clean_closed_trades_file()),
        )
        render_ai_best_action(best_action, regime=market_regime)
        render_optimizer_summary(
            summary=optimizer_summary,
            artifacts=optimizer_artifacts,
            error=optimizer_error,
        )
    else:
        if actions:
            with st.expander("\U0001f916 Auto-Trade Actions", expanded=True):
                for act in actions:
                    st.write(f"- {act}")

        if clean_export_err:
            st.warning(clean_export_err)
        else:
            st.caption(
                f"Clean closed trades exported: {clean_closed_trade_count} -> {_clean_closed_trades_file()}")

        with st.expander("AI Best Next Action", expanded=True):
            st.write(
                f"Recommendation: {best_action.get('action', 'HOLD')} {best_action.get('symbol', '-')}")
            st.write(
                f"Confidence: {float(best_action.get('confidence', 0.0)):.1f}% | Regime: {market_regime}")
            st.caption(str(best_action.get("reason", "")))

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
    
    if _use_simple_views():
        from stockmarket.views.simple_signals_tables import (
            render_live_tables_and_errors_fragment,
        )

        trade_log = list(st.session_state.get("s_log", []))
        completed_trades = _completed_trades_from_log(trade_log)
        raw_order_log = _build_raw_order_log(trade_log, _currency_symbol())
        render_live_tables_and_errors_fragment(
            buy_df=buy_df,
            sell_df=sell_df,
            holdings_df=holdings_df,
            scan_errors=scan_errors,
            trade_log=trade_log,
            completed_trades=completed_trades,
            raw_order_log=raw_order_log,
            currency_symbol=_currency_symbol(),
        )
    else:
        _fragment_live_tables_and_errors(
            buy_df,
            sell_df,
            holdings_df,
            scan_errors,
        )

    
    if auto_refresh_on:
        st.caption(f"Auto refresh active: every {refresh_seconds}s")
        _auto_refresh(refresh_seconds)


if __name__ == "__main__":
    render_simple_dashboard()
