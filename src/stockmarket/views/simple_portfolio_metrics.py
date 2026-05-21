"""Portfolio summary metrics block rendered between activity and tomorrow plan.

The view receives a frozen ``PortfolioSnapshot`` from the caller; price
refreshes and the holdings query stay in ``dashboard_simple.py``. The only
widget the view owns is the ``portfolio_metrics_tabular`` checkbox, keyed
identically to the legacy block. Views never read ``st.session_state`` and
never import ``dashboard_simple``.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from stockmarket.domain import PortfolioSnapshot


def render_portfolio_metrics(snap: PortfolioSnapshot) -> None:
    """Render the portfolio summary header, six-up (or tabular) row, and progress."""
    cs = snap.currency_symbol

    hdr, opt = st.columns([5, 1])
    with hdr:
        st.caption("Portfolio summary")
    with opt:
        table_view = st.checkbox(
            "Table view",
            key="portfolio_metrics_tabular",
            help="Show the summary row as a compact table (full numbers, no truncation).",
        )

    if table_view:
        summary_tbl = pd.DataFrame(
            [
                {
                    "Metric": "Start Capital",
                    "Amount": f"{cs} {snap.start_capital:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Current Equity",
                    "Amount": f"{cs} {snap.equity:,.2f}",
                    "Detail": f"Δ {cs} {snap.equity_delta:,.2f}",
                },
                {
                    "Metric": "Cash",
                    "Amount": f"{cs} {snap.cash:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Open PnL",
                    "Amount": f"{cs} {snap.unrealized:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Realized PnL",
                    "Amount": f"{cs} {snap.realized:,.2f}",
                    "Detail": "",
                },
                {
                    "Metric": "Total Charges",
                    "Amount": f"{cs} {snap.charges:,.2f}",
                    "Detail": f"Net {cs} {snap.net_realized:,.2f}",
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
        val = (
            "font-size:15px;font-weight:600;margin:0;line-height:1.25;"
            "white-space:normal;word-break:break-word;"
        )
        sub = "font-size:12px;margin:0;line-height:1.2;color:#198754;"
        sub_inv = "font-size:12px;margin:0;line-height:1.2;color:#dc3545;"
        a, b, c, d, e, f = st.columns(6)
        with a:
            st.markdown(
                f'<p style="{lab}">Start Capital</p>'
                f'<p style="{val}">{cs} {snap.start_capital:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with b:
            ed_col = sub if snap.equity_delta >= 0 else sub_inv
            st.markdown(
                f'<p style="{lab}">Current Equity</p>'
                f'<p style="{val}">{cs} {snap.equity:,.2f}</p>'
                f'<p style="{ed_col}">Δ {cs} {snap.equity_delta:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with c:
            st.markdown(
                f'<p style="{lab}">Cash</p>'
                f'<p style="{val}">{cs} {snap.cash:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with d:
            st.markdown(
                f'<p style="{lab}">Open PnL</p>'
                f'<p style="{val}">{cs} {snap.unrealized:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with e:
            st.markdown(
                f'<p style="{lab}">Realized PnL</p>'
                f'<p style="{val}">{cs} {snap.realized:,.2f}</p>',
                unsafe_allow_html=True,
            )
        with f:
            net_style = sub if snap.net_realized >= 0 else sub_inv
            st.markdown(
                f'<p style="{lab}">Total Charges</p>'
                f'<p style="{val}">{cs} {snap.charges:,.2f}</p>'
                f'<p style="{net_style}">Net {cs} {snap.net_realized:,.2f}</p>',
                unsafe_allow_html=True,
            )

    progress = (
        (snap.today_pnl / snap.daily_profit_target) * 100.0
        if snap.daily_profit_target > 0
        else 0.0
    )
    st.progress(
        min(1.0, max(0.0, progress / 100.0)),
        text=(
            f"Today: {cs} {snap.today_pnl:,.2f} / "
            f"{cs} {snap.daily_profit_target:,.2f} ({progress:.1f}%)"
        ),
    )
    st.caption(
        "Equity = Cash + Σ(long: last price × qty) + short unrealized PnL. "
        "Short-sale proceeds are already in Cash. "
        "Δ vs start is not equal to Open PnL + net realized unless you have no "
        "open longs and no capital adjustments. "
        f"Net (under Total Charges) = Realized PnL − charges "
        f"({cs} {snap.net_realized:,.2f})."
    )
