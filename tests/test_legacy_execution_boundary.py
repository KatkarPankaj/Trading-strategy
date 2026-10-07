import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _function_body(path: Path, name: str) -> list[ast.stmt]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return function.body


class LegacyExecutionBoundaryTests(unittest.TestCase):
    def test_root_dashboard_order_helper_fails_before_any_mutation(self):
        body = _function_body(ROOT / "dashboard.py", "_execute_paper_order")

        self.assertEqual(len(body), 1)
        self.assertIsInstance(body[0], ast.Raise)
        self.assertNotIn(
            "session_state",
            {node.attr for node in ast.walk(body[0]) if isinstance(node, ast.Attribute)},
        )

    def test_simple_dashboard_order_helper_fails_before_any_mutation(self):
        body = _function_body(ROOT / "dashboard_simple.py", "_record_trade")

        self.assertEqual(len(body), 1)
        self.assertIsInstance(body[0], ast.Raise)
        self.assertNotIn(
            "session_state",
            {node.attr for node in ast.walk(body[0]) if isinstance(node, ast.Attribute)},
        )


if __name__ == "__main__":
    unittest.main()
