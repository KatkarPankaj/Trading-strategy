from datetime import datetime, timezone
import json
import unittest

from stockmarket.core.ai import AIAnalyst, StrategySelectionSchema


NOW = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)


class StaticProvider:
    name = "test-provider"

    def __init__(self, response):
        self.response = response
        self.last_system = ""
        self.last_user = ""

    def complete(self, system, user):
        self.last_system = system
        self.last_user = user
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response(**overrides):
    value = {
        "summary": "ORB/VWAP currently fits the observed intraday breakout setup.",
        "ranked_strategies": [
            {
                "strategy": "orb_vwap",
                "confidence": 0.78,
                "rationale": "The intraday range and volume context fit this candidate.",
            },
            {
                "strategy": "mean_reversion",
                "confidence": 0.31,
                "rationale": "The current research offers weaker support.",
            },
        ],
        "risks": ["Intraday volume may be incomplete."],
        "data_gaps": ["No sector-relative strength input."],
    }
    value.update(overrides)
    return json.dumps(value)


class AIStrategySelectionTests(unittest.TestCase):
    def make_analyst(self, provider):
        return AIAnalyst(provider, clock=lambda: NOW)

    def select(self, analyst):
        return analyst.select_strategies(
            instrument_id="us-equity:NYSE:ABC",
            as_of=NOW,
            available_strategies=("orb_vwap", "mean_reversion"),
            research_context={"regime": "TRENDING_UP", "news": "No material event."},
        )

    def test_returns_ranked_advice_bound_to_request_identity_and_time(self):
        provider = StaticProvider(response())
        result, selection = self.select(self.make_analyst(provider))

        self.assertTrue(result.ok)
        self.assertIsInstance(result.output, StrategySelectionSchema)
        self.assertIsNotNone(selection)
        self.assertEqual(selection.instrument_id, "us-equity:NYSE:ABC")
        self.assertEqual(selection.as_of, NOW)
        self.assertEqual(selection.generated_at, NOW)
        self.assertEqual(selection.provider, "test-provider")
        self.assertEqual(
            [rank.strategy for rank in selection.ranked_strategies],
            ["orb_vwap", "mean_reversion"],
        )
        self.assertEqual(selection.ranked_strategies[0].confidence, 0.78)
        self.assertEqual(selection.risks, ("Intraday volume may be incomplete.",))
        self.assertFalse(hasattr(selection, "action"))
        self.assertFalse(hasattr(selection, "order"))
        self.assertIn("cannot place orders", provider.last_system)

    def test_unknown_strategy_name_is_rejected_without_a_selection(self):
        provider = StaticProvider(response(ranked_strategies=[{
            "strategy": "model_invented_strategy",
            "confidence": 0.9,
            "rationale": "Unsupported candidate.",
        }]))

        result, selection = self.select(self.make_analyst(provider))

        self.assertFalse(result.ok)
        self.assertIn("STRATEGY_SELECTION_REJECTED", result.error)
        self.assertIsNone(selection)

    def test_duplicate_rankings_are_rejected(self):
        provider = StaticProvider(response(ranked_strategies=[
            {
                "strategy": "orb_vwap",
                "confidence": 0.8,
                "rationale": "First ranking.",
            },
            {
                "strategy": "orb_vwap",
                "confidence": 0.7,
                "rationale": "Duplicate ranking.",
            },
        ]))

        result, selection = self.select(self.make_analyst(provider))

        self.assertFalse(result.ok)
        self.assertIn("duplicate strategy ranking", result.error)
        self.assertIsNone(selection)

    def test_schema_failure_and_provider_failure_fail_closed(self):
        malformed = StaticProvider(response(action="BUY"))
        failed_result, failed_selection = self.select(self.make_analyst(malformed))
        self.assertFalse(failed_result.ok)
        self.assertIn("SCHEMA_VALIDATION_FAILED", failed_result.error)
        self.assertIsNone(failed_selection)

        unavailable = StaticProvider(RuntimeError("provider offline"))
        failed_result, failed_selection = self.select(self.make_analyst(unavailable))
        self.assertFalse(failed_result.ok)
        self.assertIn("AI_PROVIDER_ERROR", failed_result.error)
        self.assertIsNone(failed_selection)

    def test_invalid_catalog_and_naive_snapshot_time_are_rejected_before_call(self):
        provider = StaticProvider(response())
        analyst = self.make_analyst(provider)
        with self.assertRaisesRegex(ValueError, "duplicates"):
            analyst.select_strategies(
                instrument_id="id",
                as_of=NOW,
                available_strategies=("orb_vwap", "orb_vwap"),
                research_context={},
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            analyst.select_strategies(
                instrument_id="id",
                as_of=datetime(2026, 10, 7, 9, 30),
                available_strategies=("orb_vwap",),
                research_context={},
            )
        self.assertEqual(provider.last_user, "")


if __name__ == "__main__":
    unittest.main()
