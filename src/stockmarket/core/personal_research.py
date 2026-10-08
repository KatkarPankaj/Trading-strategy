"""Personal recommendations: deterministic setups first, optional AI, never orders."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .ai.candidate_assessment import CandidateAssessmentService, CandidateAssessmentError
from .candidate_research import CandidateResearchService
from .data.provider import DataProviderError, DataQualityError, interval_delta
from .data.quality import validate_bars, validate_quote
from .data.resilient import ResilientProvider
from .market_session import MarketSession
from .markets import MarketRegistry, SessionPhase
from .models import Instrument, Signal, SignalSide
from .persistence import Store, to_json
from .regime import MarketRegimeEvaluator, RegimeConfig, RegimeUnavailable
from .scanner import MarketScanner, ScanMode
from .strategies.base import Strategy, StrategyMetadata

logger = logging.getLogger(__name__)


def plain(value: Any) -> Any:
    return json.loads(to_json(value))


def data_status(issues: tuple[str, ...]) -> str:
    if any("STALE" in item for item in issues):
        return "STALE"
    if "NO_DATA" in issues:
        return "MISSING"
    return "INVALID"


class PersonalResearchService:
    """Composition over existing scanner, strategies, research and assessment services."""

    def __init__(
        self, store: Store, scanner: MarketScanner, data: ResilientProvider,
        instruments: Mapping[str, Instrument], markets: MarketRegistry,
        strategies: Mapping[str, Strategy], sessions: Mapping[str, MarketSession],
        research: CandidateResearchService, assessment: CandidateAssessmentService | None,
        *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store, self.scanner, self.data = store, scanner, data
        self.instruments, self.markets = dict(instruments), markets
        self.strategies, self.sessions = dict(strategies), dict(sessions)
        self.research, self.assessment, self.clock = research, assessment, clock
        self._lock = RLock()

    def diagnostics(self, instrument_id: str, as_of: datetime | None = None) -> dict[str, Any]:
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            raise KeyError(f"unknown instrument {instrument_id!r}")
        timestamp = self._timestamp(as_of if as_of is not None else self.clock())
        market = self.markets.get(instrument.market)
        covered = market.is_covered(timestamp.astimezone(ZoneInfo(instrument.timezone)).date())
        result: dict[str, Any] = {
            "instrument_id": instrument_id, "market": instrument.market,
            "currency": instrument.currency, "asset_class": instrument.asset_class.value,
            "as_of": timestamp.isoformat(), "provider": self.data.name,
            "research_only": True, "execution": "NOT_SUBMITTED",
            "active": instrument.active, "tradable": instrument.tradable,
            "calendar_covered": covered,
            "session": market.phase(timestamp).value if covered else "UNSUPPORTED",
        }
        try:
            bars = self.data.get_ohlcv(
                instrument, "5m", timestamp - timedelta(days=5), timestamp)
            report = validate_bars(
                bars, "5m", timezone=instrument.timezone,
                now=timestamp, max_age=timedelta(minutes=15))
            result["ohlcv"] = {
                "status": "AVAILABLE" if report.ok else data_status(report.issues),
                "issues": report.issues, "bar_count": len(bars),
                "latest_bar": bars.index[-1].isoformat() if not bars.empty else None,
            }
        except DataQualityError as exc:
            result["ohlcv"] = {"status": data_status(exc.issues), "issues": exc.issues}
        except DataProviderError as exc:
            result["ohlcv"] = {
                "status": "MISSING", "reason": f"PROVIDER_UNAVAILABLE:{type(exc).__name__}"}
            logger.warning("personal research OHLCV unavailable", extra={"instrument_id": instrument_id})
        if timestamp < self.clock() - timedelta(minutes=5):
            result["quote"] = {"status": "UNSUPPORTED", "reason": "point-in-time quote is not supported"}
        else:
            try:
                quote = self.data.get_quote(instrument)
                report = validate_quote(quote, timestamp, timedelta(minutes=5))
                identity_ok = quote.instrument_id == instrument_id and quote.provider == self.data.name
                issues = report.issues + (() if identity_ok else ("QUOTE_IDENTITY_MISMATCH",))
                result["quote"] = {
                    "status": "AVAILABLE" if not issues else data_status(issues),
                    "issues": issues, "timestamp": quote.timestamp.isoformat(),
                    "price": quote.price if not issues else None,
                }
            except DataQualityError as exc:
                result["quote"] = {"status": data_status(exc.issues), "issues": exc.issues}
            except DataProviderError as exc:
                result["quote"] = {
                    "status": "MISSING", "reason": f"PROVIDER_UNAVAILABLE:{type(exc).__name__}"}
                logger.warning("personal research quote unavailable", extra={"instrument_id": instrument_id})
        result["data_quality"] = (
            "PASS" if result["ohlcv"]["status"] == "AVAILABLE"
            and result["quote"]["status"] in {"AVAILABLE", "UNSUPPORTED"} else "FAIL")
        return result

    def run(
        self, universe_id: str, idempotency_key: str, *, as_of: datetime | None = None,
        strategy_name: str = "orb_vwap", top_n: int = 10,
    ) -> dict[str, Any]:
        if strategy_name not in self.strategies:
            raise ValueError("choose a registered deterministic strategy")
        if not idempotency_key or len(idempotency_key) > 128 or any(
            not (ch.isascii() and (ch.isalnum() or ch in "_-")) for ch in idempotency_key
        ):
            raise ValueError("idempotency_key must be a safe identifier of at most 128 characters")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 10:
            raise ValueError("top_n must be between 1 and 10")
        with self._lock:
            prior = self.store.personal_research.get_by_key(idempotency_key)
            timestamp = self._timestamp(
                as_of if as_of is not None else
                datetime.fromisoformat(prior["as_of"]) if prior else None)
            request_hash = hashlib.sha256(to_json({
                "universe_id": universe_id, "as_of": timestamp,
                "strategy": strategy_name, "top_n": top_n,
            }).encode()).hexdigest()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ValueError("idempotency key belongs to a different request")
                if prior["status"] == "RUNNING":
                    raise ValueError("interrupted research run requires a new key; no execution occurred")
                return prior
            self.scanner.get_universe(universe_id)
            run_id = uuid5(NAMESPACE_URL, f"personal-research:{idempotency_key}").hex
            result: dict[str, Any] = {
                "run_id": run_id, "idempotency_key": idempotency_key,
                "request_hash": request_hash, "as_of": timestamp.isoformat(),
                "created_at": self.clock().isoformat(), "status": "RUNNING",
                "mode": "PERSONAL_RESEARCH", "execution": "NOT_SUBMITTED",
                "risk_status": "NOT_FINAL_EXECUTION_AUTHORITY",
                "label": "RECOMMENDATION ONLY", "universe_id": universe_id,
                "ai_status": "CONFIGURED" if self.assessment else "CONFIGURATION_MISSING",
                "ranking_method": "existing AI ranking for aligned actionable setups; rejected or AI-unavailable setups score zero",
                "recommendations": [],
            }
            self.store.personal_research.create(result)
            completed = False
            try:
                self._evaluate(result, timestamp, strategy_name, top_n)
                completed = True
            finally:
                if not completed:
                    result.update(status="FAILED", failure="RESEARCH_FAILED: inspect server log")
                    logger.error("personal research run failed", exc_info=True, extra={"run_id": run_id})
                self.store.personal_research.finish(result)
            return plain(result)

    def _evaluate(
        self, result: dict[str, Any], timestamp: datetime, strategy_name: str, top_n: int,
    ) -> None:
        scan = self.scanner.scan(
            result["universe_id"], ScanMode.RESEARCH, as_of=timestamp,
            top_n=top_n, scan_id=result["run_id"])
        result["scan"] = plain(scan)
        result["diagnostics"] = []
        for record in self.store.scanner_runs.candidates(scan.scan_id, limit=1000):
            payload = record["payload"]
            quality = record["quality_status"]
            instrument = self.instruments[record["instrument_id"]]
            market = self.markets.get(instrument.market)
            covered = market.is_covered(timestamp.astimezone(ZoneInfo(instrument.timezone)).date())
            status = (
                "UNSUPPORTED" if not covered else
                {"VALID": "AVAILABLE", "STALE": "STALE", "INVALID": "INVALID",
                 "INCOMPLETE": "MISSING", "UNKNOWN": "MISSING"}[quality])
            result["diagnostics"].append({
                "instrument_id": instrument.instrument_id, "market": instrument.market,
                "currency": instrument.currency, "asset_class": instrument.asset_class.value,
                "active": instrument.active, "tradable": instrument.tradable,
                "accepted": bool(record["accepted"]), "selected": bool(record["selected"]),
                "ohlcv_status": status, "data_quality": quality,
                "provider_status": payload["provider_status"],
                "data_timestamp": record["data_timestamp"],
                "session": payload["market_status"], "calendar_covered": covered,
                "reasons": payload["rejection_reasons"],
                "scan_id": scan.scan_id,
            })
        strategy = self.strategies[strategy_name]
        metadata = getattr(strategy, "metadata", None)
        signals: dict[str, Signal] = {}
        rows: dict[str, dict[str, Any]] = {}
        for candidate in scan.candidates:
            instrument = self.instruments[candidate.instrument_id]
            row: dict[str, Any] = {
                "instrument_id": instrument.instrument_id, "market": instrument.market,
                "currency": instrument.currency, "direction": "AVOID", "score": 0.0,
                "strategy": strategy_name,
                "strategy_version": metadata.version if isinstance(metadata, StrategyMetadata) else None,
                "strategy_signal": None, "ai_assessment": None,
                "ai_status": "CONFIGURATION_MISSING" if self.assessment is None else "NOT_ASSESSED",
                "as_of": timestamp.isoformat(), "data_quality": "UNKNOWN",
                "execution": "NOT_SUBMITTED", "risk_flags": [],
                "reason": "NO_SIGNAL", "evidence": None, "market_regime": None,
            }
            rows[instrument.instrument_id] = row
            if not isinstance(metadata, StrategyMetadata):
                row["reason"] = "STRATEGY_METADATA_UNAVAILABLE"
                continue
            if (
                "*" not in metadata.supported_markets and instrument.market not in metadata.supported_markets
                or instrument.asset_class not in metadata.supported_asset_classes
            ):
                row["reason"] = "STRATEGY_UNSUPPORTED_INSTRUMENT"
                continue
            session = self.sessions.get(instrument.market)
            if session is None:
                row["reason"] = "RESEARCH_SESSION_NOT_CONFIGURED"
                continue
            market = self.markets.get(instrument.market)
            day = timestamp.astimezone(ZoneInfo(instrument.timezone)).date()
            if not market.is_covered(day) or market.phase(timestamp) is not SessionPhase.REGULAR:
                row["reason"] = "UNCOVERED_OR_CLOSED_MARKET"
                continue
            interval = getattr(getattr(strategy, "config", None), "interval", None)
            if not isinstance(interval, str):
                row["reason"] = "STRATEGY_INTERVAL_UNSUPPORTED"
                continue
            step = interval_delta(interval)
            try:
                bars = self.data.get_ohlcv(
                    instrument, interval, timestamp - timedelta(days=5), timestamp)
                # Validate the full response before removing the incomplete trailing bar.
                report = validate_bars(
                    bars, interval, timezone=instrument.timezone,
                    requested_start=timestamp - timedelta(days=5), requested_end=timestamp)
                if not report.ok:
                    raise DataQualityError(report.issues)
                bars = bars.loc[bars.index + step <= timestamp].copy()
                report = validate_bars(
                    bars, interval, timezone=instrument.timezone,
                    now=timestamp, max_age=step * 2)
                if not report.ok:
                    raise DataQualityError(report.issues)
                signal = strategy.evaluate(instrument, bars, session, as_of=timestamp)
                if (signal.instrument_id != instrument.instrument_id or signal.symbol != instrument.symbol
                        or signal.strategy != strategy_name or signal.timestamp > timestamp):
                    raise ValueError("strategy returned invalid signal provenance")
                signals[instrument.instrument_id] = signal
                self.store.signals.save(signal)
                row.update(
                    strategy_signal=plain(signal), data_quality="PASS",
                    reason="; ".join(signal.reasons), latest_bar=bars.index[-1].isoformat())
                regime = MarketRegimeEvaluator(RegimeConfig(
                    interval=interval, max_bar_age=step * 2)).evaluate(
                        instrument, bars, as_of=timestamp)
                row["market_regime"] = plain(regime)
            except DataQualityError as exc:
                row.update(data_quality=data_status(exc.issues), reason=",".join(exc.issues))
            except (DataProviderError, RegimeUnavailable) as exc:
                row.update(data_quality="MISSING", reason=f"DATA_UNAVAILABLE:{type(exc).__name__}")
                logger.warning("deterministic research data unavailable",
                               extra={"instrument_id": instrument.instrument_id})

        snapshots = ()
        if scan.candidates:
            run, snapshots = self.research.run_scan(
                scan.scan_id, as_of=timestamp, limit=top_n, strategy_signals=signals)
            result["research_run"] = plain(run)
        for snapshot in snapshots:
            row = rows[snapshot.instrument_id]
            row["snapshot_id"] = snapshot.snapshot_id
            row["evidence"] = plain(snapshot)
            missing = [item.component for item in snapshot.components if item.status.value != "AVAILABLE"]
            row["risk_flags"] = [f"EVIDENCE_UNAVAILABLE:{name}" for name in missing]
            signal = signals.get(snapshot.instrument_id)
            if signal is None or row["data_quality"] != "PASS":
                continue
            actionable = signal.side is not SignalSide.HOLD and all(
                price is not None and price > 0
                for price in (signal.entry_price, signal.stop_loss, signal.take_profit, signal.reward_risk))
            if signal.side is not SignalSide.HOLD and not actionable:
                row["reason"] = "INCOMPLETE_STRATEGY_RISK_PRICES"
            if signal.side is SignalSide.SELL and self.instruments[snapshot.instrument_id].shortable is not True:
                actionable = False
                row["reason"] = "SHORT_NOT_SUPPORTED"
            technical_ok = {"technical", "regime"}.issubset({
                item.component for item in snapshot.components if item.status.value == "AVAILABLE"})
            if not technical_ok:
                actionable = False
                row["reason"] = "CRITICAL_RESEARCH_UNAVAILABLE"
            if self.assessment is not None:
                try:
                    opportunity = self.assessment.assess(
                        snapshot.snapshot_id, strategy_allowlist=(strategy_name,))
                    assessment = opportunity.assessment
                    row["ai_assessment"] = plain(assessment)
                    row["ai_status"] = assessment.status.value
                    row["risk_flags"].extend(assessment.risk_flags)
                    expected_bias = "BULLISH" if signal.side is SignalSide.BUY else "BEARISH"
                    aligned = (
                        opportunity.state.value == "STRATEGY_SELECTED"
                        and assessment.directional_bias.value == expected_bias
                        and assessment.recommended_strategy == strategy_name
                        and opportunity.ranking is not None)
                    actionable = actionable and aligned
                    if actionable and opportunity.ranking is not None:
                        row["score"] = opportunity.ranking.score
                        row["ranking"] = plain(opportunity.ranking)
                    elif not aligned:
                        row["reason"] += "; AI_REJECTED_OR_INCOMPATIBLE"
                except CandidateAssessmentError as exc:
                    row.update(ai_status="AI_INVALID", reason=f"AI_INVALID:{type(exc).__name__}")
                    actionable = False
                    logger.warning("personal AI context rejected", extra={"snapshot_id": snapshot.snapshot_id})
            elif actionable:
                row["reason"] += "; AI_CONFIGURATION_MISSING: final recommendation withheld"
                row["risk_flags"].append("AI_CONFIGURATION_MISSING")
                actionable = False
            if actionable:
                row["direction"] = "BUY" if signal.side is SignalSide.BUY else "SHORT"
        ordered = sorted(rows.values(), key=lambda row: (-row["score"], row["instrument_id"]))
        for index, row in enumerate(ordered, 1):
            row["rank"] = index
        result["recommendations"] = ordered
        result["status"] = (
            "FAILED" if not scan.candidates else
            "PARTIAL" if not snapshots or any(
                row["data_quality"] != "PASS" or row["ai_status"] != "COMPLETE"
                or row["risk_flags"] for row in ordered) else "COMPLETE")
        if not scan.candidates:
            result["failure"] = "NO_ELIGIBLE_CANDIDATES: inspect scanner diagnostics"

    def _timestamp(self, value: datetime | None) -> datetime:
        now = self.clock()
        timestamp = value if value is not None else now.replace(
            minute=now.minute - now.minute % 5, second=0, microsecond=0)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if timestamp > self.clock():
            raise ValueError("as_of cannot be in the future")
        return timestamp.astimezone(timezone.utc)
