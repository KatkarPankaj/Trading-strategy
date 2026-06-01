"""Top-of-page market session status pills (NSE / US)."""

from __future__ import annotations

from typing import Sequence

import streamlit as st

from stockmarket.market_status import MarketSessionStatus


def render_market_status_bar(
    statuses: Sequence[MarketSessionStatus],
    *,
    selected_market: str | None = None,
) -> None:
    selected = str(selected_market or "").upper()
    pills: list[str] = []
    for s in statuses:
        state = "Open" if s.is_open else "Closed"
        if s.source == "fallback":
            hint = (s.detail or "config hours").strip()
            if len(hint) > 48:
                hint = hint[:47] + "…"
            extra = f' <span class="market-status-schedule">({hint})</span>'
        else:
            extra = ""
        selected_cls = " market-status-pill-selected" if s.market_id.upper() == selected else ""
        open_cls = "market-status-open" if s.is_open else "market-status-closed"
        pills.append(
            f'<span class="market-status-pill {open_cls}{selected_cls}">'
            f"{s.label} — {state}{extra}</span>"
        )
    row = "".join(pills)
    st.markdown(
        f"""
<style>
.market-status-bar {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin: 0.25rem 0 1rem 0;
}}
.market-status-pill {{
    display: inline-block;
    padding: 0.35rem 0.75rem;
    border-radius: 999px;
    font-size: 0.9rem;
    font-weight: 500;
    border: 1px solid rgba(128, 128, 128, 0.35);
    background: rgba(128, 128, 128, 0.08);
}}
.market-status-pill-selected {{
    border-width: 2px;
    border-color: rgba(99, 102, 241, 0.65);
}}
.market-status-open {{
    color: #0d7a3e;
    border-color: rgba(13, 122, 62, 0.35);
    background: rgba(13, 122, 62, 0.1);
}}
.market-status-closed {{
    color: #8b3a3a;
    border-color: rgba(139, 58, 58, 0.35);
    background: rgba(139, 58, 58, 0.08);
}}
.market-status-schedule {{
    font-size: 0.8em;
    opacity: 0.85;
    font-weight: 400;
}}
</style>
<div class="market-status-bar">{row}</div>
        """,
        unsafe_allow_html=True,
    )
