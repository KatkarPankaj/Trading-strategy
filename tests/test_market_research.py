from datetime import datetime, timedelta, timezone
import json
import unittest

from stockmarket.core.ai import AIAnalyst
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass
from stockmarket.core.research import (
    FundamentalEvidenceProducer,
    ResearchEvidenceUnavailable,
    ResearchObservation,
    SectorEvidenceProducer,
)


AS_OF = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


def make_instrument():
    return default_markets().get("US").instrument(
        "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def score_response(**overrides):
    payload = {
        "summary": "The cited facts are moderately supportive.",
        "directional_score": 0.8,
        "confidence": 0.75,
        "risks": ["The observation has limited horizon."],
        "data_gaps": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def make_observation(instrument, component, **overrides):
    values = {
        "instrument_id": instrument.instrument_id,
        "market": instrument.market,
        "component": component,
        "subject": "Technology sector" if component == "sector" else "FY2026 results",
        "content": "Validated source facts, without a provider-specific schema.",
        "observed_at": AS_OF - timedelta(minutes=20),
        "source": "research-vendor",
        "reference": "https://example.invalid/research/123",
    }
    values.update(overrides)
    return ResearchObservation(**values)


class StaticResearchProvider:
    name = "test-research-provider"

    def __init__(self, observation=None, error=None):
        self.observation = observation
        self.error = error
        self.calls = []

    def get_observation(self, instrument, *, component, as_of, max_age):
        self.calls.append((instrument, component, as_of, max_age))
        if self.error is not None:
            raise self.error
        return self.observation


class StaticAIProvider:
    name = "test-ai"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, *_args: str):
        self.calls += 1
        if len(_args) != 2:
            raise AssertionError("AI provider must receive system and user prompts")
        return self.response


class ResearchEvidenceProducerTests(unittest.TestCase):
    def setUp(self):
        self.instrument = make_instrument()

    def producer(self, component, observation=None, response=None, error=None):
        provider = StaticResearchProvider(observation, error)
        ai_provider = StaticAIProvider(response or score_response())
        analyst = AIAnalyst(ai_provider, clock=lambda: AS_OF)
        cls = SectorEvidenceProducer if component == "sector" else FundamentalEvidenceProducer
        return cls(provider, analyst), provider, ai_provider

    def test_sector_and_fundamental_observations_produce_scoped_scores(self):
        cases = (
            ("sector", SectorEvidenceProducer, timedelta(days=1)),
            ("fundamental", FundamentalEvidenceProducer, timedelta(days=90)),
        )
        for component, producer_type, max_age in cases:
            with self.subTest(component=component):
                provider = StaticResearchProvider(
                    make_observation(self.instrument, component))
                ai_provider = StaticAIProvider(score_response())
                producer = producer_type(
                    provider, AIAnalyst(ai_provider, clock=lambda: AS_OF))

                collection = producer.collect(self.instrument, as_of=AS_OF)

                evidence, = collection.evidence
                self.assertEqual(evidence.instrument_id, self.instrument.instrument_id)
                self.assertEqual(evidence.component, component)
                self.assertAlmostEqual(evidence.score, 0.6)
                self.assertEqual(evidence.max_age, max_age)
                self.assertTrue(evidence.source.startswith(f"ai_{component}:"))
                self.assertEqual(provider.calls[0][1:], (component, AS_OF, max_age))
                self.assertEqual(collection.analyzed_count, 1)
                self.assertEqual(ai_provider.calls, 1)

    def test_missing_observation_is_reported_without_ai_score(self):
        producer, _, ai_provider = self.producer("sector")

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(collection.evidence, ())
        self.assertEqual(collection.warnings, ("NO_SECTOR_OBSERVATION",))
        self.assertEqual(ai_provider.calls, 0)

    def test_mismatched_stale_and_future_observations_fail_before_ai(self):
        invalid = (
            make_observation(self.instrument, "sector", instrument_id="OTHER"),
            make_observation(
                self.instrument, "sector", observed_at=AS_OF - timedelta(days=2)),
            make_observation(
                self.instrument, "sector", observed_at=AS_OF + timedelta(seconds=1)),
        )
        for observation in invalid:
            with self.subTest(observation=observation):
                producer, _, ai_provider = self.producer("sector", observation)
                with self.assertRaises(ResearchEvidenceUnavailable):
                    producer.collect(self.instrument, as_of=AS_OF)
                self.assertEqual(ai_provider.calls, 0)

    def test_provider_failure_is_surfaced(self):
        producer, _, ai_provider = self.producer(
            "fundamental", error=RuntimeError("offline"))

        with self.assertRaisesRegex(ResearchEvidenceUnavailable, "RuntimeError"):
            producer.collect(self.instrument, as_of=AS_OF)
        self.assertEqual(ai_provider.calls, 0)

    def test_invalid_ai_schema_yields_warning_and_no_evidence(self):
        producer, _, _ = self.producer(
            "fundamental",
            make_observation(self.instrument, "fundamental"),
            response='{"directional_score": "BUY"}',
        )

        collection = producer.collect(self.instrument, as_of=AS_OF)

        self.assertEqual(collection.evidence, ())
        self.assertIn("FUNDAMENTAL_ANALYSIS_UNAVAILABLE", collection.warnings[0])

    def test_observation_requires_bounded_cited_timezone_aware_facts(self):
        with self.assertRaisesRegex(ValueError, "6000 characters"):
            make_observation(self.instrument, "sector", content="x" * 6001)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            make_observation(
                self.instrument,
                "sector",
                observed_at=AS_OF.replace(tzinfo=None),
            )
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            make_observation(self.instrument, "sector", source=" ")


if __name__ == "__main__":
    unittest.main()
