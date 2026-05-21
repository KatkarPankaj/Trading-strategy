"""Learning Agent: Tomorrow Plan expander rendered after portfolio metrics.

The view consumes plain dicts/lists and emits Streamlit output. It never reads
``st.session_state`` and never imports ``dashboard_simple``; the caller marshals
all values in.
"""

from __future__ import annotations

from typing import Any

import streamlit as st


def render_tomorrow_plan(
    *,
    agent_plan: dict[str, Any],
    market_research: dict[str, Any],
    learning_memory: dict[str, Any],
    effective_min_buy_score: float,
    effective_min_short_score: float,
    effective_tp_pct: float,
    learning_apply_messages: list[str],
    config_guard_messages: list[str],
) -> None:
    """Render the 'Learning Agent: Tomorrow Plan' expander."""
    with st.expander("\U0001f916 Learning Agent: Tomorrow Plan", expanded=True):
        latest_learning = (
            agent_plan.get("latest_learning") if isinstance(agent_plan, dict) else None
        )
        if isinstance(latest_learning, dict):
            st.write(
                f"Latest learned day: {latest_learning.get('date', '-')} | "
                f"Closed trades: {int(latest_learning.get('closed_trades', 0))} | "
                f"Win rate: {float(latest_learning.get('win_rate', 0.0)):.1f}% | "
                f"Net: Rs {float(latest_learning.get('net', 0.0)):,.2f}"
            )
        else:
            st.write(
                "Not enough closed-trade history yet. "
                "Agent will learn as trade history grows."
            )

        st.write(
            f"Market regime: {market_research.get('regime', 'unknown')} | "
            f"Avg pchange: {float(market_research.get('avg_pchange', 0.0)):.2f}% | "
            f"Volatility proxy: {float(market_research.get('volatility', 0.0)):.2f}"
        )

        st.write(
            f"Effective thresholds now -> Buy score: {effective_min_buy_score:.1f}, "
            f"Short score: {effective_min_short_score:.1f}, "
            f"TP: {effective_tp_pct * 100.0:.2f}%"
        )

        notes = agent_plan.get("notes", []) if isinstance(agent_plan, dict) else []
        for note in notes:
            st.caption(f"- {note}")
        for msg in learning_apply_messages:
            st.caption(f"- {msg}")
        for msg in config_guard_messages:
            st.caption(f"- {msg}")

        symbol_rows = (
            learning_memory.get("symbols", [])
            if isinstance(learning_memory, dict)
            else []
        )
        if symbol_rows:
            sorted_rows = sorted(
                symbol_rows,
                key=lambda r: float(r.get("bias", 0.0) or 0.0),
                reverse=True,
            )
            top_syms = [
                str(r.get("symbol", ""))
                for r in sorted_rows[:3]
                if str(r.get("symbol", ""))
            ]
            avoid_syms = [
                str(r.get("symbol", ""))
                for r in sorted_rows[-3:]
                if str(r.get("symbol", ""))
                and float(r.get("bias", 0.0) or 0.0) < 0
            ]
            if top_syms:
                st.write(f"Preferred symbols: {', '.join(top_syms)}")
            if avoid_syms:
                st.write(f"Avoid/low-priority symbols: {', '.join(avoid_syms)}")
