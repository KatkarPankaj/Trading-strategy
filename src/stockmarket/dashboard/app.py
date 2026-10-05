"""Streamlit monitoring UI. Read-only: it cannot place or modify orders."""

from __future__ import annotations

from typing import Any, Callable

import streamlit as st

from . import views
from .client import ApiClient, ApiError

_COLORS = {"paper": "#1f6feb", "live": "#d1242f", "unknown": "#6e7781"}


def _banner(label: str, level: str) -> None:
    st.markdown(
        f"<div style='background:{_COLORS[level]};color:white;padding:12px;border-radius:6px;"
        f"text-align:center;font-size:1.4rem;font-weight:700'>{label}</div>",
        unsafe_allow_html=True)


def _safe(client: ApiClient, path: str, **params: Any) -> Any:
    try:
        return client.get(path, **params)
    except ApiError as exc:
        st.warning(str(exc))
        return None


def _table(df: Any, empty: str = "No data yet.") -> None:
    if df is None or len(df) == 0:
        st.info(empty)
    else:
        st.dataframe(df, width="stretch")


def main() -> None:
    st.set_page_config(page_title="Trading Monitor", layout="wide")
    client = ApiClient.from_env()

    try:
        health = client.get("/health")
    except ApiError as exc:
        health = None
        st.error(str(exc))
    label, level = views.mode_banner(health)
    _banner(label, level)
    st.caption(
        "Monitoring only. Orders are placed through the API, never from this page.")
    if st.sidebar.button("Refresh"):
        st.rerun()

    tabs = st.tabs(["Markets & regime", "Signals", "Portfolio", "Orders", "PnL & drawdown",
                    "Strategies", "News & AI", "Risk", "System health", "Audit log"])
    renderers: list[Callable[[], None]] = [
        lambda: _markets(client), lambda: _signals(
            client), lambda: _portfolio(client),
        lambda: _orders(client), lambda: _pnl(
            client), lambda: _strategies(client),
        lambda: _news(client), lambda: _risk(
            client), lambda: _health(health), lambda: _audit(client),
    ]
    for tab, render in zip(tabs, renderers):
        with tab:
            render()


def _markets(client: ApiClient) -> None:
    st.subheader("Global market status")
    data = _safe(client, "/markets")
    _table(views.frame(data["markets"]) if data else None)
    st.subheader("Market regime")
    signals = _safe(client, "/signals", limit=500)
    st.caption(
        "Derived from the regime recorded on recent signals; a dedicated regime engine is not wired in yet.")
    _table(views.regime_counts(signals or []))


def _signals(client: ApiClient) -> None:
    st.subheader("Top signals")
    _table(views.top_signals(_safe(client, "/signals", limit=500) or []))


def _portfolio(client: ApiClient) -> None:
    st.subheader("Portfolio")
    p = _safe(client, "/portfolio")
    if p:
        c = st.columns(4)
        c[0].metric(f"Equity ({p['base_currency']})", f"{p['equity']:,.2f}")
        c[1].metric("Daily PnL", f"{p['daily_pnl']:,.2f}")
        c[2].metric("Drawdown", f"{p['drawdown']:.2%}")
        c[3].metric("Gross exposure", f"{p['gross_exposure']:,.2f}")
        st.json({k: p[k] for k in ("cash", "sector_exposure",
                "currency_exposure", "fees", "slippage")})
    st.subheader("Open positions")
    pos = _safe(client, "/positions")
    _table(views.frame(pos["positions"])
           if pos else None, "No open positions.")


def _orders(client: ApiClient) -> None:
    st.subheader("Orders")
    only_open = st.checkbox("Open orders only")
    _table(views.frame(_safe(client, "/orders", open_only=only_open)))


def _pnl(client: ApiClient) -> None:
    st.subheader("PnL and drawdown")
    curves = views.pnl_curves(_safe(client, "/pnl") or [])
    if curves.empty:
        st.info("No PnL snapshots recorded yet.")
        return
    st.line_chart(curves[["equity"]])
    st.line_chart(curves[["drawdown"]])
    _table(curves.tail(50))


def _strategies(client: ApiClient) -> None:
    st.subheader("Strategy performance")
    _table(views.strategy_performance(
        _safe(client, "/trades", limit=1000) or []))
    perf = _safe(client, "/performance")
    if perf:
        st.json(perf["trades"])


def _news(client: ApiClient) -> None:
    st.subheader("News")
    _table(views.frame(_safe(client, "/news", limit=100)))
    st.subheader("AI explanations")
    st.caption("AI output is research input only; it never places orders.")
    _table(views.frame(_safe(client, "/ai-analyses", limit=100)))


def _risk(client: ApiClient) -> None:
    st.subheader("Risk status")
    risk = _safe(client, "/risk")
    if not risk:
        return
    st.json({"limits": risk["limits"], "current": risk["current"]})
    if risk["disabled_controls"]:
        st.warning(f"Disabled risk controls: {risk['disabled_controls']}")
    st.subheader("Rejections by reason")
    _table(views.rejection_summary(
        risk["recent_decisions"]), "No rejections recorded.")
    st.subheader("Recent risk decisions")
    _table(views.frame(risk["recent_decisions"]))


def _health(health: Any) -> None:
    st.subheader("System health")
    if not health:
        st.error("Health report unavailable.")
        return
    st.metric("Status", health["status"])
    _table(views.health_checks(health))
    st.json({k: health[k] for k in ("last_market_data_timestamp", "market_data_age_seconds",
                                    "last_successful_order", "error_counts")})


def _audit(client: ApiClient) -> None:
    st.subheader("Audit log")
    audit = _safe(client, "/audit")
    if not audit:
        return
    (st.success if audit["chain_intact"] else st.error)(
        "Audit chain intact" if audit["chain_intact"] else f"Audit chain problems: {audit['problems']}")
    _table(views.frame(audit["entries"]))
