"""
DEPRECATED — legacy complex intraday scanner UI.

The complex scanner has been removed. This module is retained only as a
deprecation stub so that ``streamlit run dashboard.py`` and any lingering
imports of ``render_complex_dashboard`` do not crash.

Use the supported app instead:
  PYTHONPATH=src streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

_LEGACY_BANNER = (
    "Legacy scanner — use `streamlit run app.py`. "
    "The complex scanner has been removed; the supported entry point is "
    "`PYTHONPATH=src streamlit run app.py` (loads dashboard_simple)."
)


def render_complex_dashboard() -> None:
    """Show the deprecation banner and stop. Kept so old callers don't ImportError."""
    st.error(_LEGACY_BANNER)
    st.stop()


# `streamlit run dashboard.py` executes this module top-to-bottom; show the
# banner immediately.
render_complex_dashboard()
