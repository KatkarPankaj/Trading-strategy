"""Slice 5 of phase 8b: legacy ``_fragment_live_tables_and_errors`` removed.

Phase 8 left the new ``views/simple_signals_tables`` path behind
``USE_SIMPLE_VIEWS=1`` while keeping the inline fragment as the default for
parity screenshots. Slice 5 hard-removes the legacy fragment and its
``_use_simple_views()`` gate; the new view is now the only path.

These tests guard against accidental reintroduction.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


DASHBOARD_PATH = Path(__file__).parent.parent / "dashboard_simple.py"


def test_legacy_fragment_live_tables_and_errors_function_removed():
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert "_fragment_live_tables_and_errors" not in source, (
        "Legacy fragment must not be reintroduced; "
        "use views/simple_signals_tables.render_live_tables_and_errors_fragment."
    )


def test_legacy_fragment_function_not_defined_via_ast():
    tree = ast.parse(DASHBOARD_PATH.read_text(encoding="utf-8"), filename=str(DASHBOARD_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert node.name != "_fragment_live_tables_and_errors"


def test_simple_signals_tables_view_is_called_unconditionally():
    """The view path is now the only path: no ``if _use_simple_views()`` gate around
    ``render_live_tables_and_errors_fragment``."""
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert "render_live_tables_and_errors_fragment" in source

    # The gating ``if _use_simple_views():`` block that wrapped the live-tables
    # call in phase 8 must no longer appear in the source. We allow other
    # incidental uses of ``_use_simple_views`` to remain (e.g. inside
    # ``_view_flag_enabled``), but not as the gate for the tables view.
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "if _use_simple_views():":
            window = "\n".join(lines[idx : idx + 20])
            assert "render_live_tables_and_errors_fragment" not in window, (
                "Legacy `if _use_simple_views()` gate around the tables view "
                "must be removed in slice 5."
            )
