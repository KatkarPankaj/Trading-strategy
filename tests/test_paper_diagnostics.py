from datetime import datetime, timezone
from unittest import TestCase

from stockmarket.core.paper_diagnostics import paper_diagnostics, summarize_cycle
from stockmarket.dashboard.views import paper_diagnostics_line

NOW = datetime(2026, 10, 15, 15, 31, tzinfo=timezone.utc)


def _run(*candidates):
    return {"run_id": "run-1", "status": "COMPLETE", "stage": "COMPLETE",
            "as_of": "2026-10-15T15:30:00+00:00", "updated_at": "2026-10-15T15:31:00+00:00",
            "universe_id": "US_ALL", "candidates": list(candidates)}


class PaperDiagnosticsTests(TestCase):
    def test_legacy_rejection_without_explanation_is_unrecorded_not_inferred(self):
        cycle = summarize_cycle(_run({"status": "RESEARCH_REJECTED", "payload": {
            "instrument_id": "ARCX:SPY", "research_error": None, "research_stage": "REJECTED"}}), now=NOW)
        self.assertEqual(cycle["primary_rejection_reason"], "UNRECORDED")
        self.assertEqual(cycle["strategy_eligibility"], "NO_CANDIDATE_SELECTED")

    def test_hold_signal_reports_breakout_reason_and_bar_age(self):
        cycle = summarize_cycle(_run({"status": "NON_ACTIONABLE", "payload": {
            "signal": {"reason": "NO_CONFIRMED_BREAKOUT",
                       "provenance": {"data_timestamp": "2026-10-15T15:25:00+00:00"},
                       "signal": {"reasons": ["NO_CONFIRMED_BREAKOUT"]}},
            "risk": {"reason": "NO_SIGNAL: HOLD/NO_TRADE signals cannot create a TradeProposal."}}}),
            now=NOW)
        self.assertEqual(cycle["primary_rejection_reason"], "NO_CONFIRMED_BREAKOUT")
        self.assertEqual(cycle["data_freshness"]["age_seconds"], 360.0)
        self.assertEqual(cycle["strategy_selected_candidates"], 1)

    def test_validation_block_is_reported_and_no_cycle_is_explicit(self):
        blocked = _run({"status": "RESEARCH_REJECTED", "payload": {
            "research_assessment": {"status": "NO_VALIDATED_STRATEGY"},
            "decision_trace": {"final_status": "REJECTED", "rejection_code": "NO_VALIDATED_STRATEGY"}}})
        report = paper_diagnostics(run=blocked, market="US", loop_status=None,
                                   ledger_orders=0, ledger_fills=0, now=NOW)
        self.assertEqual(report["automatic_entries"], "BLOCKED_NO_VALIDATED_STRATEGY")
        self.assertEqual(report["scheduler"]["state"], "DISABLED")
        self.assertIn("NO_VALIDATED_STRATEGY", paper_diagnostics_line(report))
        empty = paper_diagnostics(run=None, market="US", loop_status=None,
                                  ledger_orders=0, ledger_fills=0, now=NOW)
        self.assertEqual(empty["automatic_entries"], "NO_CYCLE_RECORDED")
        self.assertIn("none recorded", paper_diagnostics_line(empty))
