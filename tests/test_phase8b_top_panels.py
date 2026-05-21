"""Slice 1 of phase 8b: top read-only panels extracted to views.

Also adds the views-package guards (no st.session_state, no dashboard_simple
import) that subsequent slices rely on.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


VIEWS_DIR = Path(__file__).parent.parent / "src" / "stockmarket" / "views"


def _patch_streamlit() -> MagicMock:
    """Return a MagicMock standing in for the streamlit module imported by the view.

    The expander returns a context manager; everything else is a no-op MagicMock.
    """

    fake_st = MagicMock(name="streamlit")
    fake_st.expander.return_value.__enter__ = MagicMock(return_value=None)
    fake_st.expander.return_value.__exit__ = MagicMock(return_value=False)
    return fake_st


def test_simple_top_panels_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_top_panels")
    assert hasattr(mod, "render_auto_trade_actions")
    assert hasattr(mod, "render_clean_closed_trades_status")
    assert hasattr(mod, "render_ai_best_action")
    assert hasattr(mod, "render_optimizer_summary")
    assert "dashboard_simple" not in sys.modules


def test_render_auto_trade_actions_skipped_when_empty():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_auto_trade_actions([])

    fake_st.expander.assert_not_called()
    fake_st.write.assert_not_called()


def test_render_auto_trade_actions_renders_each_action():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    actions = ["BUY ACME 10@100", "SELL ACME 10@110"]
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_auto_trade_actions(actions)

    fake_st.expander.assert_called_once()
    title = fake_st.expander.call_args.args[0]
    assert "Auto-Trade Actions" in title
    assert fake_st.write.call_count == len(actions)
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert written == ["- BUY ACME 10@100", "- SELL ACME 10@110"]


def test_render_clean_closed_trades_status_caption_when_no_error():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_clean_closed_trades_status(
            count=7, error=None, path="outputs/trades.csv"
        )

    fake_st.warning.assert_not_called()
    fake_st.caption.assert_called_once()
    text = fake_st.caption.call_args.args[0]
    assert "7" in text
    assert "outputs/trades.csv" in text


def test_render_clean_closed_trades_status_warning_when_error():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_clean_closed_trades_status(
            count=0, error="boom", path="outputs/trades.csv"
        )

    fake_st.warning.assert_called_once_with("boom")
    fake_st.caption.assert_not_called()


def test_render_ai_best_action_falls_back_to_hold():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_ai_best_action({}, regime="bullish")

    fake_st.expander.assert_called_once_with("AI Best Next Action", expanded=True)
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any("HOLD" in line and "-" in line for line in written)
    assert any("Confidence: 0.0%" in line and "bullish" in line for line in written)


def test_render_ai_best_action_uses_provided_fields():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    payload = {
        "action": "BUY",
        "symbol": "ACME.NS",
        "confidence": 73.4,
        "reason": "uptrend confirmed",
    }
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_ai_best_action(payload, regime="bearish")

    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any("BUY ACME.NS" in line for line in written)
    assert any("73.4%" in line and "bearish" in line for line in written)
    fake_st.caption.assert_called_once_with("uptrend confirmed")


def test_render_optimizer_summary_warning_only_on_error_without_summary():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_optimizer_summary(
            summary=None, artifacts=None, error="optimizer crashed"
        )

    fake_st.warning.assert_called_once_with("optimizer crashed")
    fake_st.expander.assert_not_called()


def test_render_optimizer_summary_renders_summary_and_artifacts():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    summary = {
        "walkforward_status": "ok",
        "clean_closed_trades": 42,
        "trades_to_200_goal": 158,
        "trades_to_300_goal": 258,
        "baseline_net_pnl": 1234.5,
        "filtered_net_pnl": 2345.6,
        "baseline_win_rate": 0.55,
        "filtered_win_rate": 0.62,
    }
    artifacts = {"report": "outputs/report.csv", "model": "outputs/model.pkl"}

    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_optimizer_summary(
            summary=summary, artifacts=artifacts, error=None
        )

    fake_st.warning.assert_not_called()
    fake_st.expander.assert_called_once()
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any("Status: ok" in line and "42" in line for line in written)
    assert any("1,234.50" in line and "2,345.60" in line for line in written)
    assert any("55.0%" in line and "62.0%" in line for line in written)
    captioned = [c.args[0] for c in fake_st.caption.call_args_list]
    assert "- report: outputs/report.csv" in captioned
    assert "- model: outputs/model.pkl" in captioned


def test_render_optimizer_summary_no_artifacts_no_captions():
    from stockmarket.views import simple_top_panels

    fake_st = _patch_streamlit()
    with patch.object(simple_top_panels, "st", fake_st):
        simple_top_panels.render_optimizer_summary(
            summary={"clean_closed_trades": 0}, artifacts=None, error=None
        )

    fake_st.caption.assert_not_called()


# --- Views-package guards (added in slice 1, reused by later slices) -----


def _view_module_paths() -> list[Path]:
    return sorted(p for p in VIEWS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize(
    "view_path",
    _view_module_paths(),
    ids=lambda p: p.name,
)
def test_view_module_does_not_touch_session_state(view_path: Path):
    tree = ast.parse(view_path.read_text(encoding="utf-8"), filename=str(view_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "session_state":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "st":
                pytest.fail(
                    f"{view_path.name} touches st.session_state at line {node.lineno}; "
                    "views must receive plain data via parameters."
                )


@pytest.mark.parametrize(
    "view_path",
    _view_module_paths(),
    ids=lambda p: p.name,
)
def test_view_module_does_not_import_dashboard_simple(view_path: Path):
    tree = ast.parse(view_path.read_text(encoding="utf-8"), filename=str(view_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "dashboard_simple", (
                    f"{view_path.name} imports dashboard_simple; views must be standalone."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "dashboard_simple", (
                f"{view_path.name} imports from dashboard_simple at line {node.lineno}."
            )


