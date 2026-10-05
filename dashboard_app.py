"""Streamlit entry point: streamlit run dashboard_app.py (requires the API to be running)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from stockmarket.dashboard.app import main  # noqa: E402

main()
