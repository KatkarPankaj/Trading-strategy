"""Read-only top panels rendered between the trading cycle and the live tables.

Each function takes plain data (dicts / lists / scalars) and emits Streamlit
output. Views never read or write ``st.session_state``; the caller in
``dashboard_simple.py`` reads any session state and passes values in.
"""

from __future__ import annotations

from typing import Any

import streamlit as st


def render_auto_trade_actions(actions: list[str]) -> None:
    """Render the auto-trade actions expander when there are actions to show."""
    if not actions:
        return
    with st.expander("\U0001f916 Auto-Trade Actions", expanded=True):
        for act in actions:
            st.write(f"- {act}")


def render_clean_closed_trades_status(
    count: int,
    error: str | None,
    path: str,
) -> None:
    """Render the clean-closed-trades export status caption (or warning)."""
    if error:
        st.warning(error)
        return
    st.caption(f"Clean closed trades exported: {int(count)} -> {path}")


def render_ai_best_action(best_action: dict[str, Any], regime: str) -> None:
    """Render the 'AI Best Next Action' expander."""
    action = str(best_action.get("action", "HOLD"))
    symbol = str(best_action.get("symbol", "-"))
    confidence = float(best_action.get("confidence", 0.0))
    reason = str(best_action.get("reason", ""))

    with st.expander("AI Best Next Action", expanded=True):
        st.write(f"Recommendation: {action} {symbol}")
        st.write(f"Confidence: {confidence:.1f}% | Regime: {regime}")
        st.caption(reason)


def render_optimizer_summary(
    summary: dict[str, Any] | None,
    artifacts: dict[str, str] | None,
    error: str | None,
) -> None:
    """Render the optimizer error warning and/or summary expander."""
    if error:
        st.warning(error)

    if not summary:
        return

    with st.expander("\U0001f9ea Optimizer Summary", expanded=False):
        status = summary.get(
            "walkforward_status",
            summary.get("model_status", "unknown"),
        )
        st.write(
            f"Status: {status} | "
            f"Clean closed trades: {summary.get('clean_closed_trades', 0)} | "
            f"To 200: {summary.get('trades_to_200_goal', 0)} | "
            f"To 300: {summary.get('trades_to_300_goal', 0)}"
        )
        st.write(
            f"Baseline net: Rs {float(summary.get('baseline_net_pnl', 0.0)):,.2f} | "
            f"Filtered net: Rs {float(summary.get('filtered_net_pnl', 0.0)):,.2f}"
        )
        st.write(
            f"Baseline win rate: {float(summary.get('baseline_win_rate', 0.0)) * 100.0:.1f}% | "
            f"Filtered win rate: {float(summary.get('filtered_win_rate', 0.0)) * 100.0:.1f}%"
        )
        if artifacts:
            for key, value in artifacts.items():
                st.caption(f"- {key}: {value}")
