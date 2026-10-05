import unittest

from stockmarket.cli import _build_parser


class ValidationCliTests(unittest.TestCase):
    def test_validate_command_parses_walk_forward_and_grid_options(self):
        parser = _build_parser()
        args = parser.parse_args(
            [
                "validate",
                "--config",
                "config.example.json",
                "--period",
                "60d",
                "--window-mode",
                "expanding",
                "--train-sessions",
                "20",
                "--test-sessions",
                "5",
                "--step-sessions",
                "5",
                "--gap-sessions",
                "1",
                "--vwap-price-sources",
                "typical,close",
            ]
        )
        self.assertEqual(args.command, "validate")
        self.assertEqual(args.window_mode, "expanding")
        self.assertEqual(args.train_sessions, 20)
        self.assertEqual(args.test_sessions, 5)
        self.assertEqual(args.gap_sessions, 1)
        self.assertEqual(args.vwap_price_sources, "typical,close")

    def test_existing_commands_remain_registered(self):
        parser = _build_parser()
        for command in ("backtest", "signals", "sweep", "replay-best"):
            with self.subTest(command=command):
                args = parser.parse_args([command])
                self.assertEqual(args.command, command)


if __name__ == "__main__":
    unittest.main()
