import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stockmarket.core.ai.analyst import AIAnalyst, AIUnavailable
from stockmarket.core.ai.openai_compatible import OpenAICompatibleProvider
from stockmarket.core.ai.schemas import CandidateAssessmentSchema
from stockmarket.core.security import Secret


class StructuredOutputTests(unittest.TestCase):
    def provider(self, base_url="https://api.openai.com/v1"):
        return OpenAICompatibleProvider(base_url, "gpt-4.1-mini", Secret("test-key"))

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_analyst_sends_exact_strict_candidate_schema(self, transport):
        transport.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 2},
        }).encode()
        original = CandidateAssessmentSchema.model_json_schema()

        result = AIAnalyst(self.provider()).analyze("candidate_assessment", "{}")

        body = json.loads(transport.call_args.args[0].data)
        format_ = body["response_format"]
        self.assertEqual(format_["type"], "json_schema")
        self.assertTrue(format_["json_schema"]["strict"])
        schema = format_["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        scores = schema["$defs"]["ComponentScoresSchema"]
        self.assertFalse(scores["additionalProperties"])
        self.assertEqual(set(scores["required"]), {
            "technical", "regime", "news", "fundamental", "sector", "macro", "sentiment",
        })
        self.assertNotIn("momentum", scores["properties"])
        self.assertNotIn("volume", scores["properties"])
        self.assertNotIn("default", scores["properties"]["technical"])
        self.assertEqual(original, CandidateAssessmentSchema.model_json_schema())
        self.assertFalse(result.ok)
        self.assertIn("SCHEMA_VALIDATION_FAILED", result.error)
        self.assertEqual(result.prompt_tokens, 20)

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_compatible_endpoint_preserves_json_mode(self, transport):
        transport.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
        }).encode()
        AIAnalyst(self.provider("http://127.0.0.1:9000/v1")).analyze(
            "candidate_assessment", "{}")
        self.assertEqual(json.loads(transport.call_args.args[0].data)["response_format"],
                         {"type": "json_object"})

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_local_validation_still_rejects_unsupported_component_scores(self, transport):
        payload = {
            "instrument_id": "TEST:FIXTURE", "snapshot_id": "schema-check",
            "directional_bias": "INSUFFICIENT_EVIDENCE", "confidence": 0,
            "opportunity_score": 0, "risk_flags": [], "key_evidence": [],
            "invalidating_conditions": [], "explanation": "No evidence supplied.",
            "outcome": "NO_TRADE", "rejection_reasons": ["Missing evidence"],
            "component_scores": {"momentum": 0, "volume": 0},
        }
        transport.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps(payload)}}],
        }).encode()

        result = AIAnalyst(self.provider()).analyze("candidate_assessment", "{}")

        self.assertFalse(result.ok)
        self.assertIsNone(result.output)
        self.assertIn("component_scores.momentum:extra_forbidden", result.error)
        self.assertIn("component_scores.volume:extra_forbidden", result.error)

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_unsupported_schema_does_not_fall_back_to_json_mode(self, transport):
        transport.side_effect = HTTPError("https://api.openai.com", 400, "bad", {}, None)
        result = AIAnalyst(self.provider()).analyze("candidate_assessment", "{}")
        self.assertFalse(result.ok)
        self.assertIn("AIUnavailable", result.error)
        self.assertEqual(transport.call_count, 1)

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_full_schema_and_output_reserve_reject_before_transport(self, transport):
        provider = OpenAICompatibleProvider(
            "https://api.openai.com/v1", "gpt-4.1-mini", Secret("fixture"),
            context_window_tokens=5000, max_output_tokens=4096,
        )
        result = AIAnalyst(provider).analyze("candidate_assessment", "{}")
        self.assertFalse(result.ok)
        self.assertIn("AI_REQUEST_BUDGET_EXCEEDED", result.error)
        self.assertEqual(result.attempts, 0)
        transport.assert_not_called()

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_exact_serialized_budget_boundary_includes_output_and_wrappers(self, transport):
        provider = self.provider()
        body = provider._body("system", "unicode: \u20b9", {"type": "json_object"})
        required = len(body) + 64 + provider._max_output_tokens
        provider._context_window_tokens = required - 1
        with self.assertRaisesRegex(AIUnavailable, "AI_REQUEST_BUDGET_EXCEEDED"):
            provider.complete_with_usage("system", "unicode: \u20b9")
        transport.assert_not_called()
        provider._context_window_tokens = required
        transport.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
        }).encode()
        provider.complete_with_usage("system", "unicode: \u20b9")
        self.assertEqual(transport.call_args.args[0].data, body)

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_cumulative_budget_admission_includes_full_request_and_output(self, transport):
        result = AIAnalyst(self.provider(), token_budget=4096).analyze(
            "candidate_assessment", "{}")
        self.assertEqual(result.error, "AI_BUDGET_EXHAUSTED")
        transport.assert_not_called()

    @patch("stockmarket.core.ai.openai_compatible.urlopen")
    def test_oversized_input_is_rejected_not_silently_truncated(self, transport):
        analyst = AIAnalyst(self.provider(), max_input_chars=1000)
        result = analyst.analyze("candidate_assessment", "x" * 1001)
        self.assertEqual(result.error, "AI_INPUT_LIMIT_EXCEEDED")
        context_result = analyst.analyze("candidate_assessment", "{}",
                                         context={"value": "x" * 2001})
        self.assertEqual(context_result.error, "AI_CONTEXT_LIMIT_EXCEEDED")
        transport.assert_not_called()
