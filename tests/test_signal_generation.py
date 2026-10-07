from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta, timezone
import json
from unittest.mock import patch

import pandas as pd

from stockmarket.core.market_session import MarketSession
from stockmarket.core.markets import default_markets
from stockmarket.core.models import AssetClass, Signal, SignalSide
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.signal_generation import (
    SignalGenerationService,
    SignalGenerationSettings,
    parse_signal_generation_settings,
)
from stockmarket.core.persistence.repositories import to_json
from stockmarket.core.strategies.orb_vwap import OrbVwapConfig, OrbVwapStrategy


ZONE = "America/New_York"
EVALUATION_AS_OF = datetime(2026, 10, 5, 9, 50, tzinfo=timezone(
    timedelta(hours=-4)))
OPPORTUNITY_AS_OF = EVALUATION_AS_OF - timedelta(minutes=5)
SNAPSHOT_ID = "snapshot-1"
OPPORTUNITY_ID = "opportunity-1"
RUN_ID = "run-1"
INSTRUMENT = default_markets().get("US").instrument(
    "AAPL", mic="XNAS", asset_class=AssetClass.EQUITY, tick_size=0.01)


def _bars() -> pd.DataFrame:
    index = pd.date_range(
        "2026-10-05 09:30", periods=4, freq="5min", tz=ZONE).tz_convert("UTC")
    close = [100.0, 100.1, 100.4, 101.0]
    return pd.DataFrame(
        {
            "open": close,
            "high": [100.15, 100.2, 100.5, 101.1],
            "low": [99.9, 100.0, 100.3, 100.9],
            "close": close,
            "volume": [100.0, 100.0, 100.0, 500.0],
        },
        index=index,
    )


class _MarketData:
    name = "test-provider"
    research_only = True

    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars
        self.calls = 0

    def get_ohlcv(self, instrument, interval, start, end):
        if instrument.instrument_id != INSTRUMENT.instrument_id or interval != "5m":
            raise ValueError("unexpected market-data request")
        self.calls += 1
        return self.bars.loc[(self.bars.index >= start) & (self.bars.index <= end)]


class _AutonomousRepository:
    def __init__(self, opportunity: dict) -> None:
        self.candidate = {
            "run_id": RUN_ID,
            "instrument_id": INSTRUMENT.instrument_id,
            "stage": "STRATEGY_SELECTED",
            "snapshot_id": SNAPSHOT_ID,
            "opportunity_id": OPPORTUNITY_ID,
            "payload": {
                "instrument_id": INSTRUMENT.instrument_id,
                "snapshot_id": SNAPSHOT_ID,
                "stage": "STRATEGY_SELECTED",
                "opportunity": opportunity,
            },
        }

    def get_run(self, run_id):
        return {"run_id": run_id, "status": "COMPLETE",
                "stage": "STRATEGY_SELECTED",
                "as_of": OPPORTUNITY_AS_OF.isoformat(),
                "payload": {"request": {
                    "mode": "PAPER",
                    "as_of": OPPORTUNITY_AS_OF.isoformat(),
                    "markets": ["US"],
                    "asset_classes": ["EQUITY"],
                    "strategy_allowlist": ["orb_vwap"],
                }}} \
            if run_id == RUN_ID else None

    def get_candidate(self, run_id, candidate_id):
        if run_id != RUN_ID or candidate_id not in (
                INSTRUMENT.instrument_id, OPPORTUNITY_ID):
            return None
        return self.candidate


class _ResearchRepository:
    def __init__(self) -> None:
        self.snapshot = {
            "snapshot_id": SNAPSHOT_ID,
            "instrument_id": INSTRUMENT.instrument_id,
            "market": INSTRUMENT.market,
            "payload": {
                "snapshot_id": SNAPSHOT_ID,
                "as_of": OPPORTUNITY_AS_OF.isoformat(),
                "created_at": (EVALUATION_AS_OF - timedelta(minutes=4)).isoformat(),
                "market": INSTRUMENT.market,
            },
        }

    def get_snapshot(self, snapshot_id):
        return self.snapshot if snapshot_id == SNAPSHOT_ID else None


class _OpportunityRepository:
    def __init__(self, opportunity: dict) -> None:
        self.opportunity = {
            "opportunity_id": OPPORTUNITY_ID,
            "state": "STRATEGY_SELECTED",
            "payload": opportunity,
        }

    def get_opportunity(self, opportunity_id):
        return self.opportunity if opportunity_id == OPPORTUNITY_ID else None


class _SignalRepository:
    def __init__(self, autonomous_repository: _AutonomousRepository) -> None:
        self.autonomous_repository = autonomous_repository
        self.results = {}
        self.by_candidate = {}

    def get_by_key(self, key):
        return self.results.get(key)

    def latest_for_candidate(self, run_id, candidate_id):
        return self.by_candidate.get((run_id, candidate_id))

    def save_result(self, **values):
        payload = json.loads(to_json(values["payload"]))
        row = {"payload": payload, "evaluation_as_of": values[
            "evaluation_as_of"].isoformat()}
        self.results[values["idempotency_key"]] = row
        if values["status"] == "SIGNAL_GENERATED":
            self.autonomous_repository.candidate["stage"] = "SIGNAL_GENERATED"
            self.by_candidate[(values["run_id"], values["candidate_id"])] = row
        return row


class SignalGenerationTests(unittest.TestCase):
    def setUp(self):
        strategy = OrbVwapStrategy(OrbVwapConfig(
            volume_ma_window=2,
            max_bar_age=timedelta(minutes=5),
        ))
        opportunity = {
            "opportunity_id": OPPORTUNITY_ID,
            "state": "STRATEGY_SELECTED",
            "created_at": (EVALUATION_AS_OF - timedelta(minutes=2)).isoformat(),
            "assessment": {
                "snapshot_id": SNAPSHOT_ID,
                "instrument_id": INSTRUMENT.instrument_id,
                "status": "COMPLETE",
                "assessed_at": (EVALUATION_AS_OF - timedelta(minutes=3)).isoformat(),
                "recommended_strategy": strategy.name,
                "input_context": {
                    "snapshot_id": SNAPSHOT_ID,
                    "snapshot_created_at": (
                        EVALUATION_AS_OF - timedelta(minutes=4)).isoformat(),
                    "instrument_id": INSTRUMENT.instrument_id,
                    "symbol": INSTRUMENT.symbol,
                    "market": INSTRUMENT.market,
                    "asset_class": INSTRUMENT.asset_class.value,
                    "currency": INSTRUMENT.currency,
                    "timezone": INSTRUMENT.timezone,
                    "as_of": OPPORTUNITY_AS_OF.isoformat(),
                    "registered_strategies": [{
                        "name": strategy.name,
                        "implementation": type(strategy).__name__,
                        "version": strategy.version,
                    }],
                },
            },
        }
        self.autonomous_repository = _AutonomousRepository(opportunity)
        self.research_repository = _ResearchRepository()
        self.signal_repository = _SignalRepository(self.autonomous_repository)
        self.market_data = _MarketData(_bars())
        self.strategy = strategy
        self.service = SignalGenerationService(
            self.market_data,
            self.autonomous_repository,
            self.research_repository,
            _OpportunityRepository(opportunity),
            self.signal_repository,
            {INSTRUMENT.instrument_id: INSTRUMENT},
            default_markets(),
            {strategy.name: strategy},
            {
                "US": MarketSession(
                    timezone=ZONE,
                    market_open=time(9, 30),
                    opening_range_end=time(9, 40),
                    entry_cutoff=time(15, 0),
                    square_off=time(15, 55),
                    market_close=time(16, 0),
                ),
            },
            clock=lambda: datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc),
        )

    def test_generates_and_persists_deterministic_signal_once(self):
        first = self.service.generate(
            RUN_ID, INSTRUMENT.instrument_id,
            evaluation_as_of=EVALUATION_AS_OF,
        )
        second = self.service.generate(
            RUN_ID, INSTRUMENT.instrument_id,
            evaluation_as_of=EVALUATION_AS_OF,
        )

        self.assertEqual(first["status"], "SIGNAL_GENERATED")
        self.assertEqual(first["signal_type"], "BUY")
        self.assertEqual(first["signal"]["signal_id"], second["signal"]["signal_id"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.market_data.calls, 1)
        self.assertEqual(self.autonomous_repository.candidate["stage"],
                         "SIGNAL_GENERATED")
        self.assertEqual(first["execution"], "NOT_SUBMITTED")
        self.assertEqual(first["risk_status"], "NOT_EVALUATED")

    def test_rejects_unaware_and_future_evaluation_timestamps(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.service.generate(
                RUN_ID, INSTRUMENT.instrument_id,
                evaluation_as_of=datetime(2026, 10, 5, 9, 50),
            )
        result = self.service.generate(
            RUN_ID, INSTRUMENT.instrument_id,
            evaluation_as_of=EVALUATION_AS_OF + timedelta(hours=3),
        )

        self.assertEqual(result["reason"], "FUTURE_EVALUATION_TIMESTAMP")
        self.assertEqual(self.market_data.calls, 0)

    def test_rejects_strategy_catalog_version_mismatch_before_data_access(self):
        self.opportunity_version = "unspecified"
        self.autonomous_repository.candidate["payload"]["opportunity"][
            "assessment"]["input_context"]["registered_strategies"][0][
                "version"] = self.opportunity_version
        self.signal_repository.autonomous_repository = self.autonomous_repository

        result = self.service.generate(
            RUN_ID, INSTRUMENT.instrument_id,
            evaluation_as_of=EVALUATION_AS_OF,
        )

        self.assertEqual(result["reason"], "STRATEGY_VERSION_MISMATCH")
        self.assertEqual(self.market_data.calls, 0)

    def test_rejects_stale_bars_without_advancing_candidate(self):
        self.market_data.bars = self.market_data.bars.iloc[:-2]

        result = self.service.generate(
            RUN_ID, INSTRUMENT.instrument_id,
            evaluation_as_of=EVALUATION_AS_OF,
        )

        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["reason"], "MARKET_DATA_STALE")
        self.assertEqual(self.autonomous_repository.candidate["stage"],
                         "STRATEGY_SELECTED")

    def test_hold_is_persisted_as_no_signal_without_execution(self):
        hold = Signal(
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            timestamp=_bars().index[-1].to_pydatetime(),
            strategy=self.strategy.name,
            side=SignalSide.HOLD,
            reasons=("NO_SETUP",),
        )
        with patch.object(self.strategy, "evaluate", return_value=hold):
            result = self.service.generate(
                RUN_ID, INSTRUMENT.instrument_id,
                evaluation_as_of=EVALUATION_AS_OF,
            )

        self.assertEqual(result["status"], "SIGNAL_GENERATED")
        self.assertEqual(result["signal_type"], "NO_SIGNAL")
        self.assertEqual(result["signal"]["side"], "HOLD")
        self.assertEqual(result["execution"], "NOT_SUBMITTED")
        self.assertEqual(result["risk_status"], "NOT_EVALUATED")

    def test_settings_are_bounded_and_reject_unknown_fields(self):
        settings = parse_signal_generation_settings(
            '{"max_opportunity_age_seconds": 300}')
        self.assertEqual(settings, SignalGenerationSettings(
            max_opportunity_age=timedelta(seconds=300)))
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            parse_signal_generation_settings('{"unlimited": true}')
        with self.assertRaisesRegex(ValueError, "604800"):
            parse_signal_generation_settings(
                '{"max_market_data_age_seconds": 604801}')

    def test_repository_atomically_persists_signal_and_candidate_transition(self):
        db = SQLiteDatabase()
        self.addCleanup(db.close)
        migrate(db)
        store = Store(db)
        now = EVALUATION_AS_OF.isoformat()
        db.execute(
            """INSERT INTO scanner_runs
               (scan_id, universe_id, markets, mode, started_at, completed_at,
                status, requested_count, evaluated_count, accepted_count,
                rejected_count, failed_count, failure_summary, payload, as_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("scan", "universe", "[]", "RESEARCH", now, now, "COMPLETE",
             1, 1, 1, 0, 0, "[]", "{}", now),
        )
        db.execute(
            """INSERT INTO research_runs
               (run_id, scan_id, as_of, created_at, status, requested_count,
                completed_count, failed_count, failure_summary, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("research", "scan", now, now, "COMPLETE", 1, 1, 0, "[]", "{}"),
        )
        db.execute(
            """INSERT INTO research_snapshots
               (snapshot_id, run_id, instrument_id, scanner_rank,
                scanner_score, as_of, status, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (SNAPSHOT_ID, "research", INSTRUMENT.instrument_id, 1,
             50.0, now, "COMPLETE", "{}"),
        )
        db.execute(
            """INSERT INTO ai_research_assessments
               (assessment_id, snapshot_id, status, provider, model_version,
                prompt_version, schema_version, assessed_at, strategy_valid,
                recommended_strategy, error, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("assessment", SNAPSHOT_ID, "COMPLETE", "test", "1", "1", "1",
             now, 1, "orb_vwap", None, "{}"),
        )
        db.execute(
            """INSERT INTO research_opportunities
               (opportunity_id, assessment_id, state, ranking_score,
                lifecycle, created_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (OPPORTUNITY_ID, "assessment", "STRATEGY_SELECTED", 50.0,
             "[]", now, "{}"),
        )
        db.execute(
            """INSERT INTO autonomous_research_runs
               (run_id, idempotency_key, request_hash, status, stage,
                created_at, updated_at, as_of, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (RUN_ID, "key", "hash", "COMPLETE", "STRATEGY_SELECTED",
             now, now, now, "{}"),
        )
        db.execute(
            """INSERT INTO autonomous_research_candidates
               (run_id, instrument_id, stage, snapshot_id, opportunity_id,
                error, updated_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (RUN_ID, INSTRUMENT.instrument_id, "REJECTED", SNAPSHOT_ID,
             OPPORTUNITY_ID, None, now, to_json({"stage": "REJECTED"})),
        )
        signal = Signal(
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            timestamp=EVALUATION_AS_OF,
            strategy="orb_vwap",
            side=SignalSide.HOLD,
        )
        arguments = {
            "generation_id": "generation-1",
            "idempotency_key": "idempotency-1",
            "run_id": RUN_ID,
            "candidate_id": INSTRUMENT.instrument_id,
            "opportunity_id": OPPORTUNITY_ID,
            "snapshot_id": SNAPSHOT_ID,
            "instrument_id": INSTRUMENT.instrument_id,
            "strategy_name": "orb_vwap",
            "strategy_version": "1.0.0",
            "evaluation_as_of": EVALUATION_AS_OF,
            "data_timestamp": EVALUATION_AS_OF,
            "input_fingerprint": "a" * 64,
            "status": "SIGNAL_GENERATED",
            "reason": "NO_SETUP",
            "signal": signal,
            "generated_at": EVALUATION_AS_OF,
            "payload": {"generation_id": "generation-1", "signal": signal},
        }
        with self.assertRaisesRegex(RuntimeError, "STRATEGY_SELECTED"):
            store.signal_generations.save_result(**arguments)
        self.assertEqual(db.query("SELECT * FROM signals"), [])
        self.assertEqual(db.query("SELECT * FROM generated_strategy_signals"), [])

        db.execute(
            """UPDATE autonomous_research_candidates
               SET stage = 'STRATEGY_SELECTED', payload = ?
               WHERE run_id = ? AND instrument_id = ?""",
            (to_json({"stage": "STRATEGY_SELECTED"}),
             RUN_ID, INSTRUMENT.instrument_id),
        )
        saved = store.signal_generations.save_result(**arguments)
        repeated = store.signal_generations.save_result(**arguments)
        self.assertEqual(saved["generation_id"], repeated["generation_id"])
        self.assertTrue(repeated["payload"]["duplicate"])
        candidate = store.autonomous_research.get_candidate(
            RUN_ID, INSTRUMENT.instrument_id)
        self.assertEqual(candidate["stage"], "SIGNAL_GENERATED")
        self.assertEqual(len(db.query("SELECT * FROM signals")), 1)
        self.assertEqual(len(db.query("SELECT * FROM generated_strategy_signals")), 1)


if __name__ == "__main__":
    unittest.main()
