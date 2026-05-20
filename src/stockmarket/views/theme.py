"""Streamlit appearance helpers (minimal layout CSS)."""

from __future__ import annotations

import streamlit as st


def inject_theme() -> None:
    """Inject minimal global CSS (font size, caption wrapping)."""
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
