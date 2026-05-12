"""Streamlit appearance helpers (light/dark-friendly CSS)."""

from __future__ import annotations

import streamlit as st


def inject_theme() -> None:
    """Sidebar appearance + minimal CSS that works in light and dark Streamlit themes."""
    appearance = st.sidebar.selectbox(
        "Appearance",
        ["System default", "Light", "Dark"],
        index=0,
        key="app_appearance_mode",
    )
    is_dark = appearance == "Dark"

    if is_dark:
        st.markdown(
            """
<style>
    .stApp { font-size: 15px; }
    div[data-testid="stExpander"] summary { font-weight: 600; }
    .stCaption, [data-testid="stCaption"] {
        white-space: normal;
        word-wrap: break-word;
        overflow: visible;
    }
</style>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
<style>
    .stApp { font-size: 15px; }
    .stCaption, [data-testid="stCaption"] {
        white-space: normal;
        word-wrap: break-word;
        overflow: visible;
    }
</style>
            """,
            unsafe_allow_html=True,
        )
