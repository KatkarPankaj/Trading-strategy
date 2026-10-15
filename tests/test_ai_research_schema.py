"""Milestone 2 P4: AI research schema extensions and cost controls."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
import unittest

from pydantic import ValidationError

from stockmarket.core.ai import AIAnalyst
from stockmarket.core.ai.analyst import AIUnavailable
from stockmarket.core.ai.candidate_assessment import CandidateAssessmentService
from stockmarket.core.ai.schemas import CandidateAssessmentSchema
from stockmarket.core.candidate_research import (
    CandidateResearchService,
    CandidateResearchSettings,
)
from stockmarket.core.markets import default_markets
from stockmarket.core.strategies import OrbVwapStrategy

from test_candidate_research import (
    AS_OF,
    AssessmentProvider,
    StaticObservationProvider,
    assessment_response,
    make_fixture,
    strategy_response,
)


class UsageProvider:
    name = "usage-test-provider"

    def __init__(self, responses, usage=None):
        self.responses = list(responses)
        self.usage = usage
        self.calls = 0

    def complete_with_usage(self, _system, _user):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response), self.usage

    def complete(self, system, user):
        return self.complete_with_usage(system, user)[0]


class SchemaTests(unittest.TestCase):
    def test_legacy_v1_output_remains_valid_with_safe_defaults(self):
        parsed = CandidateAssessmentSchema.model_validate(assessment_response())
        self.assertEqual(parsed.outcome, "CANDIDATE")
        self.assertEqual(parsed.contradictory_evidence, [])
        self.assertIsNone(parsed.evidence_quality)

    def test_no_trade_requires_rejection_reasons(self):
        with self.assertRaises(ValidationError):
            CandidateAssessmentSchema.model_validate(
                assessment_response(outcome="NO_TRADE"))

    def test_candidate_cannot_carry_rejection_reasons(self):
        with self.assertRaises(ValidationError):
            CandidateAssessmentSchema.model_validate(
                assessment_response(rejection_reasons=["weak"]))

    def test_evidence_cannot_be_supporting_and_contradictory(self):
        with self.assertRaises(ValidationError):
            CandidateAssessmentSchema.model_validate(assessment_response(
                key_evidence=["e1"], contradictory_evidence=["e1"]))

    def test_component_scores_are_bounded(self):
        with self.assertRaises(ValidationError):
            CandidateAssessmentSchema.model_validate(assessment_response(
                component_scores={"technical": 1.5}))

    def test_model_cannot_supply_urls_or_calibrated_probability(self):
        for extra in ({"sources": [{"url": "https://x"}]},
                      {"calibrated_probability": 0.9}):
            with self.assertRaises(ValidationError):
                CandidateAssessmentSchema.model_validate(assessment_response(**extra))


class AssessmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.instrument, self.market_data, self.store, _ = make_fixture()
        research = CandidateResearchService(
            self.market_data,
            self.store.scanner_runs,
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            fundamental_provider=StaticObservationProvider(),
            settings=CandidateResearchSettings(cache_ttl_seconds=0),
            clock=lambda: AS_OF,
        )
        _, snapshots = research.run_scan("scan-test")
        self.snapshot = snapshots[0]
        self.evidence = self.store.research_runs.evidence(self.snapshot.snapshot_id)

    def _evidence_id(self, component):
        return next(item["evidence_id"] for item in self.evidence
                    if item["component"] == component)

    def _service(self, provider, **analyst_kwargs):
        return CandidateAssessmentService(
            AIAnalyst(provider, clock=lambda: AS_OF + timedelta(seconds=1),
                      **analyst_kwargs),
            self.store.research_runs,
            {self.instrument.instrument_id: self.instrument},
            default_markets(),
            {"orb_vwap": OrbVwapStrategy()},
            clock=lambda: AS_OF + timedelta(seconds=2),
        )

    def _response(self, **overrides):
        return assessment_response(
            instrument_id=self.snapshot.instrument_id,
            snapshot_id=self.snapshot.snapshot_id,
            **overrides)

    def _order_count(self):
        return self.store.db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"]

    def test_no_trade_is_rejected_without_strategy_selection(self):
        provider = AssessmentProvider([self._response(
            outcome="NO_TRADE", rejection_reasons=["Earnings event within session."])])

        opportunity = self._service(provider).assess(self.snapshot.snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(opportunity.assessment.status.value, "NO_TRADE")
        self.assertIsNone(opportunity.assessment.recommended_strategy)
        self.assertEqual(opportunity.assessment.rejection_reasons,
                         ("Earnings event within session.",))
        self.assertIn("NO_TRADE", opportunity.assessment.error)
        self.assertEqual(len(provider.users), 1)
        self.assertEqual(self._order_count(), 0)

    def test_qualified_negative_outcomes_override_positive_ai_selection_without_order(self):
        from stockmarket.core.outcome_feedback import OutcomeFeedbackService
        from stockmarket.core.strategy_ranking import StrategyRankingService
        from stockmarket.core.strategies.base import strategy_parameters_hash
        provider = AssessmentProvider([self._response(), strategy_response()])
        service = self._service(provider)
        strategy = service.strategies["orb_vwap"]
        for i in range(30):
            opened = AS_OF - timedelta(days=40 - i)
            self.store.paper_outcomes.save({
                "outcome_id": f"negative-{i}", "instrument_id": self.instrument.instrument_id,
                "market": self.instrument.market, "currency": self.instrument.currency,
                "opened_at": opened.isoformat(), "closed_at": (opened + timedelta(minutes=5)).isoformat(),
                "fill_ids": [f"fixture-{i}"], "entry_notional": 1000,
                "gross_pnl": -8, "fees": 2, "net_pnl": -10, "attribution_status": "COMPLETE",
                "attribution": {"strategy_id": "orb_vwap", "strategy_version": strategy.version,
                                "parameters_hash": strategy_parameters_hash(strategy),
                                "timeframe": strategy.config.interval, "market_regime": "UNKNOWN"}},
                opened + timedelta(minutes=10))
        ranker = StrategyRankingService(self.store, OutcomeFeedbackService(self.store, clock=lambda: AS_OF),
                                        clock=lambda: AS_OF + timedelta(seconds=2))
        ranker.strategy_source = lambda: service.strategies
        service.strategy_ranker = ranker.rank
        opportunity = service.assess(self.snapshot.snapshot_id)
        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(opportunity.assessment.status.value, "NO_TRADE")
        self.assertIsNone(opportunity.assessment.recommended_strategy)
        self.assertIn("below the NO_TRADE", opportunity.assessment.error)
        self.assertEqual(self._order_count(), 0)

    def test_insufficient_evidence_quality_is_safe_rejection(self):
        provider = AssessmentProvider([self._response(evidence_quality="INSUFFICIENT")])

        opportunity = self._service(provider).assess(self.snapshot.snapshot_id)

        self.assertEqual(opportunity.state.value, "REJECTED")
        self.assertEqual(opportunity.assessment.status.value, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(len(provider.users), 1)

    def test_uncited_contradictory_evidence_fails_closed(self):
        provider = AssessmentProvider([self._response(
            contradictory_evidence=["not-in-this-snapshot"])])

        opportunity = self._service(provider).assess(self.snapshot.snapshot_id)

        self.assertEqual(opportunity.assessment.status.value, "AI_INVALID")
        self.assertIsNone(opportunity.ranking)

    def test_extended_fields_sources_and_contradiction_penalty_are_persisted(self):
        technical = self._evidence_id("technical")
        fundamental = self._evidence_id("fundamental")
        provider = AssessmentProvider([
            self._response(
                key_evidence=[technical],
                contradictory_evidence=[fundamental],
                evidence_quality="MEDIUM",
                qualitative_confidence="MEDIUM",
                component_scores={"technical": 0.6, "fundamental": -0.3},
            ),
            strategy_response(),
        ])
        service = self._service(provider)

        opportunity = service.assess(self.snapshot.snapshot_id)
        assessment = opportunity.assessment

        self.assertEqual(assessment.status.value, "COMPLETE", assessment.error)
        self.assertIsNone(assessment.calibrated_probability)
        self.assertEqual(assessment.qualitative_confidence, "MEDIUM")
        self.assertEqual(assessment.component_scores["technical"], 0.6)
        self.assertIsNone(assessment.component_scores["news"])
        urls = {item["evidence_id"]: item["url"] for item in assessment.sources}
        self.assertEqual(urls[fundamental], "https://example.invalid/earnings")
        self.assertIsNone(urls[technical])
        # One risk flag and one contradiction, each penalised by 5 points.
        self.assertEqual(opportunity.ranking.risk_penalty, 10.0)
        self.assertIn("contradictory_evidence=1", opportunity.ranking.explanation)
        self.assertEqual(assessment.ai_usage["calls"], 2)
        persisted = self.store.research_runs.get_assessment(assessment.assessment_id)
        self.assertEqual(persisted["payload"]["contradictory_evidence"], [fundamental])
        self.assertIsNone(persisted["payload"]["calibrated_probability"])
        self.assertEqual(self._order_count(), 0)


class AnalystCostControlTests(unittest.TestCase):
    def test_transient_failures_retry_within_limit(self):
        sleeps = []
        provider = UsageProvider([
            AIUnavailable("timeout", retryable=True),
            assessment_response(),
        ], usage={"prompt_tokens": 120, "completion_tokens": 30})
        analyst = AIAnalyst(provider, max_retries=1, sleep=sleeps.append,
                            retry_backoff_seconds=0.5)

        result = analyst.analyze("candidate_assessment", "{}")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (120, 30))
        self.assertFalse(result.usage_estimated)
        self.assertEqual(analyst.usage.prompt_tokens, 120)

    def test_retries_stop_at_limit(self):
        provider = UsageProvider([AIUnavailable("x", retryable=True)] * 3)
        analyst = AIAnalyst(provider, max_retries=1, sleep=lambda _s: None)

        result = analyst.analyze("candidate_assessment", "{}")

        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("AI_PROVIDER_ERROR"))
        self.assertEqual(provider.calls, 2)

    def test_non_retryable_failure_is_not_retried(self):
        provider = UsageProvider([AIUnavailable("HTTP 401"), assessment_response()])
        analyst = AIAnalyst(provider, max_retries=3, sleep=lambda _s: None)

        result = analyst.analyze("candidate_assessment", "{}")

        self.assertFalse(result.ok)
        self.assertEqual(provider.calls, 1)

    def test_token_budget_exhaustion_fails_closed_without_calling_provider(self):
        provider = UsageProvider(
            [assessment_response(), assessment_response()],
            usage={"prompt_tokens": 900, "completion_tokens": 200})
        analyst = AIAnalyst(provider, token_budget=1500)

        first = analyst.analyze("candidate_assessment", "{}")
        second = analyst.analyze("candidate_assessment", "{}")

        self.assertTrue(first.ok, first.error)
        self.assertFalse(second.ok)
        self.assertEqual(second.error, "AI_BUDGET_EXHAUSTED")
        self.assertEqual(provider.calls, 1)
        self.assertTrue(analyst.usage.budget_exhausted is False
                        or analyst.usage.prompt_tokens == 900)

    def test_usage_is_estimated_when_provider_reports_none(self):
        provider = AssessmentProvider([assessment_response()])
        analyst = AIAnalyst(provider)

        result = analyst.analyze("candidate_assessment", "{}")

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.usage_estimated)
        self.assertGreater(result.prompt_tokens, 0)
        self.assertEqual(analyst.usage.estimated_calls, 1)

    def test_concurrent_requests_reserve_budget_before_provider_call(self):
        entered, release = Event(), Event()

        class BlockingProvider(UsageProvider):
            def request_token_reserve(self, system, user, task, schema):
                return 100, 100

            def complete_with_usage(self, system, user):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test provider release timed out")
                return super().complete_with_usage(system, user)

        provider = BlockingProvider(
            [assessment_response(), assessment_response()],
            usage={"prompt_tokens": 100, "completion_tokens": 100})
        analyst = AIAnalyst(provider, token_budget=264)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(analyst.analyze, "candidate_assessment", "{}")
            try:
                self.assertTrue(entered.wait(5))
                second = analyst.analyze("candidate_assessment", "{}")
            finally:
                release.set()
            result = first.result(timeout=5)
        self.assertFalse(second.ok)
        self.assertEqual(second.error, "AI_BUDGET_EXHAUSTED")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(analyst.usage.calls, 1)

    def test_failed_request_releases_inflight_reservation(self):
        class ReservedProvider(UsageProvider):
            def request_token_reserve(self, system, user, task, schema):
                return 100, 100

        provider = ReservedProvider(
            [AIUnavailable("test failure"), assessment_response()],
            usage={"prompt_tokens": 100, "completion_tokens": 100})
        analyst = AIAnalyst(provider, token_budget=264)
        self.assertFalse(analyst.analyze("candidate_assessment", "{}").ok)
        second = analyst.analyze("candidate_assessment", "{}")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(provider.calls, 2)

    def test_invalid_cost_settings_are_rejected(self):
        for kwargs in ({"max_retries": 4}, {"max_retries": -1},
                       {"token_budget": 0}, {"retry_backoff_seconds": -1}):
            with self.assertRaises(ValueError):
                AIAnalyst(AssessmentProvider([]), **kwargs)


if __name__ == "__main__":
    unittest.main()
