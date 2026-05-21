"""Activity & Logs panel rendered between the cycle and the live tables.

The view takes pre-normalized step records and the live-mode booleans and emits
the same Processing/Idle header + scrollable activity box the legacy block
produced. Views never read ``st.session_state`` and never import
``dashboard_simple``; the caller passes everything in.
"""

from __future__ import annotations

import html
from typing import Callable

import streamlit as st


def render_activity_and_logs(
    *,
    steps: list[dict[str, str]],
    auto_refresh_on: bool,
    auto_trade_on: bool,
    on_manual_refresh: Callable[[], None],
) -> None:
    """Render the Activity expander with Processing/Idle title and refresh button.

    Parameters mirror what ``dashboard_simple`` used to compute inline before
    phase 8b:

    - ``steps``: normalized activity rows (``{"ts": str, "msg": str}``).
    - ``auto_refresh_on`` / ``auto_trade_on``: drive the "Processing" vs "Idle"
      header.
    - ``on_manual_refresh``: invoked when the user clicks "Refresh now"
      (the caller wires this to ``st.rerun``).
    """
    live = bool(auto_refresh_on or auto_trade_on)
    title = "Processing" if live else "Idle"
    dot = "#198754" if live else "#dc3545"

    latest_preview = ""
    if steps:
        last = steps[-1]
        latest_preview = f"{last['ts']} — {last['msg']}"

    preview_html = ""
    if latest_preview:
        preview_html = (
            '<br/><span style="color:#495057;font-size:13px;">'
            + html.escape(latest_preview)
            + "</span>"
        )

    if steps:
        parts: list[str] = []
        for entry in steps:
            ts_e = html.escape(entry["ts"])
            msg_e = html.escape(entry["msg"])
            parts.append(
                f'<div style="line-height:1.35;"><strong>{ts_e}</strong> — {msg_e}</div>'
            )
        lines_html = "".join(parts)
    else:
        lines_html = (
            '<div style="line-height:1.35;color:#6c757d;">'
            "No activity steps recorded yet.</div>"
        )

    scroll_box = (
        '<div style="line-height:1.35;font-size:14px;color:#212529;margin-top:6px;">'
        '<div style="color:#6c757d;font-size:12px;margin-bottom:4px;">'
        "Activity (newest last)</div>"
        '<div style="'
        "overflow-y:auto;overflow-x:auto;resize:vertical;"
        "height:15lh;min-height:8rem;max-height:40rem;"
        "box-sizing:border-box;border:1px solid #dee2e6;border-radius:6px;"
        "padding:8px 10px;background:#fafafa;"
        '">'
        f"{lines_html}</div></div>"
    )

    with st.expander("Activity", expanded=False):
        hdr_l, hdr_r = st.columns([4, 1])
        with hdr_l:
            st.markdown(
                f'<p style="font-size:14px;line-height:1.35;margin:0;">'
                f'<span style="color:{dot};font-weight:700;">●</span>'
                f'<span style="font-weight:600;"> {title}</span>'
                f"{preview_html}"
                f"</p>",
                unsafe_allow_html=True,
            )
        with hdr_r:
            if st.button(
                "Refresh now",
                key="simple_manual_refresh",
                help="Full dashboard rerun (quotes, ranking, auto-trade).",
            ):
                on_manual_refresh()

        st.html(scroll_box)
