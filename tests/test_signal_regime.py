from datetime import datetime, timezone
from unittest import TestCase

from stockmarket.core.signal_generation import _snapshot_regime

AS_OF = datetime(2026, 10, 15, 19, 41, tzinfo=timezone.utc)


def evidence(**overrides):
    item = {"component": "regime", "instrument_id": "XNAS:QQQ", "quality": "VALID",
            "observed_at": "2026-10-15T00:00:00+00:00", "payload": {"label": "TRENDING_UP"}}
    return {"evidence": [{**item, **overrides}]}


class SnapshotRegimeTests(TestCase):
    def test_valid_point_in_time_regime_is_used(self):
        self.assertEqual(_snapshot_regime(evidence(), "XNAS:QQQ", AS_OF), "TRENDING_UP")

    def test_untrusted_regime_evidence_stays_unknown(self):
        for bad in (evidence(instrument_id="ARCX:SPY"), evidence(quality="STALE"),
                    evidence(observed_at="2026-10-16T00:00:00+00:00"),
                    evidence(payload={"label": "TO_THE_MOON"}), evidence(observed_at="garbage"),
                    {"evidence": []}, {}):
            self.assertIsNone(_snapshot_regime(bad, "XNAS:QQQ", AS_OF), bad)
