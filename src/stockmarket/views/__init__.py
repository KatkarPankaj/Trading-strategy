"""Views package for Streamlit UI components."""

from .simple_signals_tables import render_live_tables_and_errors_fragment
from .simple_top_panels import (
    render_ai_best_action,
    render_auto_trade_actions,
    render_clean_closed_trades_status,
    render_optimizer_summary,
)
from .theme import inject_theme

__all__ = [
    "render_live_tables_and_errors_fragment",
    "render_auto_trade_actions",
    "render_clean_closed_trades_status",
    "render_ai_best_action",
    "render_optimizer_summary",
    "inject_theme",
]
