"""Auto-refresh footer rendered at the bottom of the simple dashboard.

The view is pure: it emits a caption when auto-refresh is enabled and a small
JS snippet that asks Streamlit's parent frame to rerun the script after the
configured delay. Side effects that mutate session state (like the per-cycle
price refresh) stay in the caller; the view never reads ``st.session_state``
and never imports ``dashboard_simple``.
"""

from __future__ import annotations

import streamlit as st


def render_auto_refresh_footer(*, enabled: bool, refresh_seconds: int) -> None:
    """Render the "Auto refresh active" caption and JS rerun snippet."""
    if not enabled:
        return
    st.caption(f"Auto refresh active: every {refresh_seconds}s")
    if refresh_seconds <= 0:
        return
    st.html(
        f"""
        <script>
            (function() {{
                const delayMs = {int(refresh_seconds) * 1000};
                setTimeout(function() {{
                    try {{
                        window.parent.postMessage({{isStreamlitMessage: true, type: 'streamlit:rerunScript'}}, '*');
                    }} catch (e) {{
                        try {{ window.location.reload(); }} catch (ee) {{}}
                    }}
                }}, delayMs);
            }})();
        </script>
        """
    )
