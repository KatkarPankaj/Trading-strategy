"""Slice 2 of phase 8b: Activity & Logs panel extracted to views.

The global views/* AST guards in ``test_phase8b_top_panels.py`` already cover
``simple_activity_and_logs.py`` (they parametrize over every file in the views
package). These focused tests pin the new module's contract:

- import isolation (no ``dashboard_simple`` import, no ``st.session_state`` reads),
- the ``USE_SIMPLE_VIEWS_ACTIVITY_LOGS`` flag falls back to ``USE_SIMPLE_VIEWS``,
- render-side smoke tests covering Processing/Idle header, empty steps placeholder,
  and the manual-refresh callback wiring.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


VIEW_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "stockmarket"
    / "views"
    / "simple_activity_and_logs.py"
)


def _patch_streamlit() -> MagicMock:
    fake_st = MagicMock(name="streamlit")
    fake_st.expander.return_value.__enter__ = MagicMock(return_value=None)
    fake_st.expander.return_value.__exit__ = MagicMock(return_value=False)
    col = MagicMock()
    col.__enter__ = MagicMock(return_value=None)
    col.__exit__ = MagicMock(return_value=False)
    fake_st.columns.return_value = (col, col)
    fake_st.button.return_value = False
    return fake_st


def test_simple_activity_and_logs_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_activity_and_logs")
    assert hasattr(mod, "render_activity_and_logs")
    assert "dashboard_simple" not in sys.modules


def test_simple_activity_and_logs_does_not_touch_session_state():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "session_state":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "st":
                raise AssertionError(
                    f"simple_activity_and_logs touches st.session_state at line {node.lineno}"
                )


def test_simple_activity_and_logs_does_not_import_dashboard_simple():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "dashboard_simple"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "dashboard_simple"


def test_view_flag_enabled_helper_falls_back_to_master_for_activity_logs(monkeypatch):
    sys.modules.pop("dashboard_simple", None)
    import dashboard_simple

    monkeypatch.delenv("USE_SIMPLE_VIEWS", raising=False)
    monkeypatch.delenv("USE_SIMPLE_VIEWS_ACTIVITY_LOGS", raising=False)
    assert dashboard_simple._view_flag_enabled("ACTIVITY_LOGS") is False

    monkeypatch.setenv("USE_SIMPLE_VIEWS", "1")
    assert dashboard_simple._view_flag_enabled("ACTIVITY_LOGS") is True

    monkeypatch.setenv("USE_SIMPLE_VIEWS_ACTIVITY_LOGS", "0")
    assert dashboard_simple._view_flag_enabled("ACTIVITY_LOGS") is False

    monkeypatch.delenv("USE_SIMPLE_VIEWS", raising=False)
    monkeypatch.setenv("USE_SIMPLE_VIEWS_ACTIVITY_LOGS", "1")
    assert dashboard_simple._view_flag_enabled("ACTIVITY_LOGS") is True


def test_render_activity_and_logs_idle_title_when_live_off():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=[],
            auto_refresh_on=False,
            auto_trade_on=False,
            on_manual_refresh=lambda: None,
        )

    fake_st.expander.assert_called_once_with("Activity", expanded=False)
    markdown_text = fake_st.markdown.call_args.args[0]
    assert "Idle" in markdown_text
    assert "#dc3545" in markdown_text
    html_text = fake_st.html.call_args.args[0]
    assert "No activity steps recorded yet." in html_text


def test_render_activity_and_logs_processing_title_when_auto_refresh():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=[],
            auto_refresh_on=True,
            auto_trade_on=False,
            on_manual_refresh=lambda: None,
        )

    markdown_text = fake_st.markdown.call_args.args[0]
    assert "Processing" in markdown_text
    assert "#198754" in markdown_text


def test_render_activity_and_logs_renders_each_step_and_latest_preview():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    steps = [
        {"ts": "09:15:01", "msg": "Cycle start"},
        {"ts": "09:15:42", "msg": "Ranked 12 signals"},
    ]
    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=steps,
            auto_refresh_on=False,
            auto_trade_on=True,
            on_manual_refresh=lambda: None,
        )

    markdown_text = fake_st.markdown.call_args.args[0]
    assert "Processing" in markdown_text
    assert "09:15:42 — Ranked 12 signals" in markdown_text

    html_text = fake_st.html.call_args.args[0]
    assert "09:15:01" in html_text and "Cycle start" in html_text
    assert "09:15:42" in html_text and "Ranked 12 signals" in html_text
    assert "No activity steps recorded yet." not in html_text


def test_render_activity_and_logs_escapes_step_html():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    steps = [{"ts": "10:00:00", "msg": "<script>alert('x')</script>"}]
    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=steps,
            auto_refresh_on=True,
            auto_trade_on=False,
            on_manual_refresh=lambda: None,
        )

    html_text = fake_st.html.call_args.args[0]
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_render_activity_and_logs_invokes_callback_when_button_clicked():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    fake_st.button.return_value = True
    callback = MagicMock()

    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=[],
            auto_refresh_on=False,
            auto_trade_on=False,
            on_manual_refresh=callback,
        )

    fake_st.button.assert_called_once()
    button_kwargs = fake_st.button.call_args.kwargs
    assert button_kwargs.get("key") == "simple_manual_refresh"
    callback.assert_called_once_with()


def test_render_activity_and_logs_does_not_invoke_callback_without_click():
    from stockmarket.views import simple_activity_and_logs

    fake_st = _patch_streamlit()
    fake_st.button.return_value = False
    callback = MagicMock()

    with patch.object(simple_activity_and_logs, "st", fake_st):
        simple_activity_and_logs.render_activity_and_logs(
            steps=[],
            auto_refresh_on=False,
            auto_trade_on=False,
            on_manual_refresh=callback,
        )

    callback.assert_not_called()
