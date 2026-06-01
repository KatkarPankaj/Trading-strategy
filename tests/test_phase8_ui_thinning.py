from __future__ import annotations

import importlib
import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_simple_signals_view_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_signals_tables")
    assert hasattr(mod, "render_live_tables_and_errors_fragment")
    assert "dashboard_simple" not in sys.modules


def test_cycle_factory_builds_services_with_sqlite_repo(tmp_path, monkeypatch):
    from stockmarket.cycle.factory import build_services
    from stockmarket.ml.null_scorer import NullSymbolScorer

    monkeypatch.chdir(tmp_path)
    services = build_services(
        session={},
        market_now_fn=lambda: datetime(2026, 1, 1, 9, 30, 0),
        market_open=time(9, 15),
        entry_cutoff=time(13, 30),
        square_off=time(15, 15),
        charges_fn=lambda _side, _value: 0.0,
        rank_signals_fn=lambda **_kwargs: (
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            [],
        ),
        refresh_prices_fn=lambda: None,
        persist_state_fn=lambda: None,
        market="NSE",
        scorer=NullSymbolScorer(),
    )

    assert services.repo.__class__.__name__ == "SqlitePaperRepo"
    assert services.signals.__class__.__name__ == "DashboardSignalSource"


def test_dashboard_legacy_warning_present():
    src = Path(__file__).parent.parent / "dashboard.py"
    content = src.read_text(encoding="utf-8")
    assert "Legacy scanner — use `streamlit run app.py`" in content


def test_webapp_legacy_warning_present():
    src = Path(__file__).parent.parent / "src" / "stockmarket" / "webapp.py"
    content = src.read_text(encoding="utf-8")
    assert "Legacy research UI." in content
