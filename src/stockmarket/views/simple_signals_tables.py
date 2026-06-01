"""Simple dashboard signal and portfolio table rendering."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st


@st.fragment
def render_live_tables_and_errors_fragment(
    *,
    buy_df: pd.DataFrame,
    sell_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    scan_errors: list[str],
    trade_log: list[dict[str, Any]],
    completed_trades: pd.DataFrame,
    raw_order_log: pd.DataFrame,
    currency_symbol: str,
) -> None:
    feed_errors = [
        e for e in scan_errors if str(e).startswith("NSE feed:")
    ]
    if feed_errors:
        st.error(feed_errors[0])

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🔥 Top 5 Buy Signals")
        if buy_df.empty:
            st.info("No buy candidates in selected price band.")
        else:
            show_buy = buy_df[
                [
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
                ]
            ].copy()
            show_buy["price"] = show_buy["price"].map(lambda x: f"Rs {float(x):.2f}")
            show_buy["rank_order_value"] = show_buy["rank_order_value"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_buy["rank_expected_profit"] = show_buy["rank_expected_profit"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_buy["rank_symbol_headroom"] = show_buy["rank_symbol_headroom"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_buy["rank_deployment_headroom"] = show_buy[
                "rank_deployment_headroom"
            ].map(lambda x: f"Rs {float(x):,.0f}")
            st.dataframe(
                show_buy,
                width="stretch",
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("Symbol", width="small"),
                    "price": st.column_config.TextColumn("Price", width="small"),
                    "rank_qty": st.column_config.NumberColumn(
                        "Qty", format="%d", width="small"
                    ),
                    "rank_order_value": st.column_config.TextColumn(
                        "Order value", width="medium"
                    ),
                    "rank_expected_profit": st.column_config.TextColumn(
                        "Expected profit", width="medium"
                    ),
                    "rank_symbol_headroom": st.column_config.TextColumn(
                        "Symbol headroom", width="medium"
                    ),
                    "rank_deployment_headroom": st.column_config.TextColumn(
                        "Deployment headroom", width="medium"
                    ),
                    "buy_score": st.column_config.NumberColumn(
                        "Score", format="%.1f", width="small"
                    ),
                    "research_buy_score": st.column_config.NumberColumn(
                        "Research", format="%.1f", width="small"
                    ),
                    "research_tag": st.column_config.TextColumn("Tag", width="medium"),
                    "effective_buy_score": st.column_config.NumberColumn(
                        "Effective", format="%.1f", width="small"
                    ),
                    "buy_signal": st.column_config.TextColumn("Signal", width="small"),
                    "pchange": st.column_config.NumberColumn(
                        "%Chg", format="%.2f", width="small"
                    ),
                    "updated": st.column_config.TextColumn("Updated", width="medium"),
                },
            )

    with c2:
        st.subheader("🧊 Top 5 Sell Signals")
        if sell_df.empty:
            st.info("No sell candidates in selected price band.")
        else:
            show_sell = sell_df[
                [
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
                ]
            ].copy()
            show_sell["price"] = show_sell["price"].map(lambda x: f"Rs {float(x):.2f}")
            show_sell["rank_order_value"] = show_sell["rank_order_value"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_sell["rank_expected_profit"] = show_sell["rank_expected_profit"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_sell["rank_symbol_headroom"] = show_sell["rank_symbol_headroom"].map(
                lambda x: f"Rs {float(x):,.0f}"
            )
            show_sell["rank_deployment_headroom"] = show_sell[
                "rank_deployment_headroom"
            ].map(lambda x: f"Rs {float(x):,.0f}")
            st.dataframe(
                show_sell,
                width="stretch",
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("Symbol", width="small"),
                    "price": st.column_config.TextColumn("Price", width="small"),
                    "rank_qty": st.column_config.NumberColumn(
                        "Qty", format="%d", width="small"
                    ),
                    "rank_order_value": st.column_config.TextColumn(
                        "Order value", width="medium"
                    ),
                    "rank_expected_profit": st.column_config.TextColumn(
                        "Expected profit", width="medium"
                    ),
                    "rank_symbol_headroom": st.column_config.TextColumn(
                        "Symbol headroom", width="medium"
                    ),
                    "rank_deployment_headroom": st.column_config.TextColumn(
                        "Deployment headroom", width="medium"
                    ),
                    "sell_score": st.column_config.NumberColumn(
                        "Score", format="%.1f", width="small"
                    ),
                    "research_sell_score": st.column_config.NumberColumn(
                        "Research", format="%.1f", width="small"
                    ),
                    "research_tag": st.column_config.TextColumn("Tag", width="medium"),
                    "effective_sell_score": st.column_config.NumberColumn(
                        "Effective", format="%.1f", width="small"
                    ),
                    "sell_signal": st.column_config.TextColumn("Signal", width="small"),
                    "pchange": st.column_config.NumberColumn(
                        "%Chg", format="%.2f", width="small"
                    ),
                    "updated": st.column_config.TextColumn("Updated", width="medium"),
                },
            )

    st.subheader("📈 Open Positions")
    if holdings_df.empty:
        st.info("No open positions")
    else:
        view_h = holdings_df.copy()
        preferred_cols = [
            "side",
            "symbol",
            "qty",
            "avg",
            "ltp",
            "stop",
            "target",
            "invested",
            "pnl",
            "pnl_pct",
        ]
        present_cols = [c for c in preferred_cols if c in view_h.columns]
        if present_cols:
            view_h = view_h[present_cols]
        for col in ["avg", "ltp", "stop", "target", "invested", "pnl"]:
            view_h[col] = view_h[col].map(lambda x: f"Rs {float(x):.2f}")
        if "pnl_pct" in view_h.columns:
            view_h["pnl_pct"] = view_h["pnl_pct"].map(lambda x: f"{float(x):.2f}%")
        st.dataframe(
            view_h,
            width="stretch",
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

    st.subheader("📒 Trade History")
    log_df = pd.DataFrame(trade_log)

    if completed_trades.empty:
        st.info("No completed trades yet")
    else:
        trade_col_map: list[tuple[str, str]] = [
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
        sorted_trades = completed_trades.sort_values("timestamp", ascending=False)
        order_src = [src for src, _ in trade_col_map if src in sorted_trades.columns]
        rename = {src: dst for src, dst in trade_col_map if src in sorted_trades.columns}
        show_history = sorted_trades[order_src].rename(columns=rename)

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
                return "—"
            line = t.splitlines()[0].strip()
            if len(line) > 120:
                return line[:119] + "…"
            return line

        money_cols = [
            "Buying price",
            "Selling price",
            "Charges",
            "Total invested",
            "Total collected",
            "Realized P&L",
            "Cash after trade",
        ]
        for col in money_cols:
            if col in show_history.columns:
                show_history[col] = show_history[col].map(lambda x: f"Rs {float(x):,.2f}")

        if "Quantity" in show_history.columns:
            show_history["Quantity"] = show_history["Quantity"].map(lambda x: int(float(x)))

        if "Reason" in show_history.columns:
            show_history["Reason"] = show_history["Reason"].map(_fmt_reason_cell)

        pnl_label = "Realized P&L"

        def _highlight_trade_pnl(s: pd.Series) -> list[str]:
            return [
                "color: #16a34a"
                if _rs_amount(v) > 0
                else "color: #dc2626"
                if _rs_amount(v) < 0
                else ""
                for v in s
            ]

        st.dataframe(
            show_history.style.apply(_highlight_trade_pnl, subset=[pnl_label]),
            width="stretch",
            hide_index=True,
            column_config={
                "Timestamp": st.column_config.TextColumn("Timestamp", width="medium"),
                "Symbol": st.column_config.TextColumn("Symbol", width="small"),
                "Side": st.column_config.TextColumn("Side", width="small"),
                "Quantity": st.column_config.NumberColumn(
                    "Quantity", format="%d", width="small"
                ),
                "Buying price": st.column_config.TextColumn(
                    "Buying price", width="small"
                ),
                "Selling price": st.column_config.TextColumn(
                    "Selling price", width="small"
                ),
                "Charges": st.column_config.TextColumn("Charges", width="small"),
                "Total invested": st.column_config.TextColumn(
                    "Total invested", width="medium"
                ),
                "Total collected": st.column_config.TextColumn(
                    "Total collected", width="medium"
                ),
                pnl_label: st.column_config.TextColumn(pnl_label, width="medium"),
                "Reason": st.column_config.TextColumn("Reason", width="large"),
                "Cash after trade": st.column_config.TextColumn(
                    "Cash after trade", width="medium"
                ),
            },
        )

    if not log_df.empty:
        with st.expander("Raw order log (all legs)", expanded=False):
            if raw_order_log.empty:
                st.info("No raw trade legs yet.")
            else:
                def _fmt_money(val: object) -> str:
                    if val is None or (isinstance(val, float) and pd.isna(val)):
                        return "—"
                    return f"{currency_symbol} {float(val):,.2f}"

                money_cols = [
                    "Buy price",
                    "Buy amount",
                    "Sell price",
                    "Sell amount",
                    "Profit/Loss amount",
                ]
                view_log = raw_order_log.copy()
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
                        "Timestamp": st.column_config.TextColumn(
                            "Timestamp", width="medium"
                        ),
                        "Symbol": st.column_config.TextColumn("Symbol", width="small"),
                        "Side": st.column_config.TextColumn("Side", width="small"),
                        "Qty": st.column_config.NumberColumn(
                            "Qty", format="%d", width="small"
                        ),
                        "Buy price": st.column_config.TextColumn(
                            "Buy price", width="small"
                        ),
                        "Buy amount": st.column_config.TextColumn(
                            "Buy amount", width="medium"
                        ),
                        "Sell price": st.column_config.TextColumn(
                            "Sell price", width="small"
                        ),
                        "Sell amount": st.column_config.TextColumn(
                            "Sell amount", width="medium"
                        ),
                        "Profit/Loss amount": st.column_config.TextColumn(
                            "Profit/Loss amount", width="medium"
                        ),
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
