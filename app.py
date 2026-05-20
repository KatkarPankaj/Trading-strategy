"""
Unified launcher: Simple dashboard only.

Run from repo root: PYTHONPATH=src streamlit run app.py

``.streamlit/config.toml`` enables ``runOnSave``: after you save a ``.py`` file, the
app reruns in the browser without restarting the Streamlit process.

The legacy complex scanner lives in dashboard.py and is not loaded from this entry point.
"""

from __future__ import annotations

import json as _json_dbg
import os
import sys
import time as _time_dbg
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st


def _aglog(hid: str, loc: str, msg: str, data: dict) -> None:
    if not os.environ.get("TRADING_DEBUG_AGENT_LOG"):
        return
    try:
        log_path = _ROOT / ".cursor" / "debug-ee38d1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as _f:
            _f.write(
                _json_dbg.dumps(
                    {
                        "sessionId": "ee38d1",
                        "runId": "verify-fix",
                        "hypothesisId": hid,
                        "location": loc,
                        "message": msg,
                        "data": data,
                        "timestamp": int(_time_dbg.time() * 1000),
                    }
                )
                + "\n"
            )
    except Exception:
        pass


try:
    from stockmarket.views.theme import inject_theme
except Exception:

    def inject_theme() -> None:  # type: ignore[misc]
        return None


def main() -> None:
    st.set_page_config(
        page_title="Trading Dashboard",
        layout="wide",
        page_icon="📊",
    )
    inject_theme()
    st.sidebar.info(
        "**Complex scanner** is deprecated for this unified app. "
        "Only the Simple dashboard runs here. "
        "Legacy UI: `streamlit run dashboard.py` (unsupported)."
    )
    _aglog(
        "H1",
        "app.py:import_simple",
        "before_dashboard_simple",
        {"py": list(sys.version_info[:3])},
    )
    from dashboard_simple import render_simple_dashboard

    render_simple_dashboard(standalone=False)


if __name__ == "__main__":
    main()
