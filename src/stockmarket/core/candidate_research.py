"""Timestamped, source-attributed candidate research; this module cannot execute trades."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
from math import isfinite, sqrt
from threading import RLock
from time import monotonic
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd

from ..news import NewsEvent, NewsProvider, NewsQuery
from .data.quality import validate_bars
from .data.provider import DataQualityError, MarketDataProvider, interval_delta
from .models import Instrument, Signal
from .regime import MarketRegimeEvaluator, RegimeConfig, RegimeUnavailable
from .research import ResearchEvidence, ResearchObservation, ResearchObservationProvider

logger = logging.getLogger(__name__)


class EvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    REJECTED = "REJECTED"


class EvidenceQuality(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class _EvidenceRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResearchEvidenceRecord:
    evidence_id: str
    instrument_id: str
    component: str
    as_of: datetime
    observed_at: datetime | None
    retrieved_at: datetime
    source: str
    quality: EvidenceQuality
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "instrument_id", "component", "source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("as_of", "retrieved_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.observed_at is not None and (
                self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None):
            raise ValueError("observed_at must be timezone-aware or None")
        if self.observed_at is not None and self.observed_at > self.as_of:
            raise ValueError("evidence cannot be observed after as_of")
        if not isinstance(self.quality, EvidenceQuality):
            raise ValueError("quality must be an EvidenceQuality")
        if not isinstance(self.payload, Mapping) or not isinstance(self.provenance, Mapping):
            raise TypeError("payload and provenance must be mappings")


@dataclass(frozen=True, slots=True)
class ComponentAssessment:
    component: str
    status: EvidenceStatus
    evidence_count: int
    source: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    snapshot_id: str
    run_id: str
    instrument_id: str
    symbol: str
    market: str
    scanner_rank: int
    scanner_score: float
    as_of: datetime
    created_at: datetime
    status: str
    components: tuple[ComponentAssessment, ...]
    scores: tuple[ResearchEvidence, ...]
    evidence: tuple[ResearchEvidenceRecord, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateResearchRun:
    run_id: str
    scan_id: str
    as_of: datetime
    created_at: datetime
    status: str
    requested_count: int
    completed_count: int
    failed_count: int
    snapshot_ids: tuple[str, ...]
    failure_summary: tuple[str, ...] = ()
    configuration_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class CandidateResearchSettings:
    interval: str = "1d"
    lookback_days: int = 120
    regime_lookback_bars: int = 20
    regime_max_bar_age: timedelta = timedelta(days=5)
    fundamental_max_age: timedelta = timedelta(days=90)
    sector_max_age: timedelta = timedelta(days=1)
    news_max_age: timedelta = timedelta(hours=72)
    news_current_window: timedelta = timedelta(minutes=5)
    max_concurrency: int = 4
    max_candidates: int = 50
    cache_ttl_seconds: float = 30.0
    cache_capacity: int = 256

    def __post_init__(self) -> None:
        interval_delta(self.interval)
        if isinstance(self.regime_lookback_bars, bool) \
                or not isinstance(self.regime_lookback_bars, int) \
                or not 2 <= self.regime_lookback_bars <= 500:
            raise ValueError("regime_lookback_bars must be between 2 and 500")
        for name in ("lookback_days", "max_concurrency", "max_candidates", "cache_capacity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "regime_max_bar_age", "fundamental_max_age", "sector_max_age",
            "news_max_age", "news_current_window",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        if isinstance(self.cache_ttl_seconds, bool) \
                or not isinstance(self.cache_ttl_seconds, (int, float)) \
                or not isfinite(self.cache_ttl_seconds) or self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be finite and non-negative")
        if self.max_concurrency > 32 or self.lookback_days > 3650 \
                or self.max_candidates > 50 or self.cache_capacity > 4096:
            raise ValueError("candidate research settings exceed safe resource limits")


def parse_candidate_research_settings(raw: str | None) -> CandidateResearchSettings:
    if raw is None or not raw.strip():
        return CandidateResearchSettings()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CANDIDATE_RESEARCH_SETTINGS must be valid JSON") from exc
    allowed = {
        "interval", "lookback_days", "regime_lookback_bars",
        "regime_max_bar_age_seconds", "fundamental_max_age_seconds",
        "sector_max_age_seconds", "news_max_age_seconds",
        "news_current_window_seconds", "max_concurrency", "max_candidates",
        "cache_ttl_seconds", "cache_capacity",
    }
    if not isinstance(payload, dict) or set(payload).difference(allowed):
        raise ValueError("CANDIDATE_RESEARCH_SETTINGS contains unsupported fields")
    values = dict(payload)
    for field, seconds_name in (
        ("regime_max_bar_age", "regime_max_bar_age_seconds"),
        ("fundamental_max_age", "fundamental_max_age_seconds"),
        ("sector_max_age", "sector_max_age_seconds"),
        ("news_max_age", "news_max_age_seconds"),
        ("news_current_window", "news_current_window_seconds"),
    ):
        seconds = values.pop(seconds_name, None)
        if seconds is not None:
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                    or not isfinite(seconds) or seconds <= 0:
                raise ValueError(f"{seconds_name} must be finite and positive")
            values[field] = timedelta(seconds=seconds)
    return CandidateResearchSettings(**values)


class CandidateResearchService:
    """Research accepted scanner candidates without creating signals or execution requests."""

    def __init__(
        self,
        market_data: MarketDataProvider,
        scanner_runs: Any,
        research_runs: Any,
        instruments: Mapping[str, Instrument],
        *,
        news_provider: NewsProvider | None = None,
        fundamental_provider: ResearchObservationProvider | None = None,
        sector_provider: ResearchObservationProvider | None = None,
        settings: CandidateResearchSettings | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.market_data = market_data
        self.scanner_runs = scanner_runs
        self.research_runs = research_runs
        self.instruments = dict(instruments)
        self.news_provider = news_provider
        self.fundamental_provider = fundamental_provider
        self.sector_provider = sector_provider
        self.settings = settings or CandidateResearchSettings()
        self.clock = clock
        self._cache: OrderedDict[tuple[str, str, str], tuple[float, tuple[Any, ...]]] = OrderedDict()
        self._cache_lock = RLock()

    def run_scan(
        self,
        scan_id: str,
        *,
        as_of: datetime | None = None,
        limit: int = 50,
        run_id: str | None = None,
        max_concurrency: int | None = None,
        strategy_signals: Mapping[str, Signal] | None = None,
    ) -> tuple[CandidateResearchRun, tuple[ResearchSnapshot, ...]]:
        if not isinstance(scan_id, str) or not scan_id.strip():
            raise ValueError("scan_id must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) \
                or not 1 <= limit <= self.settings.max_candidates:
            raise ValueError(
                f"limit must be between 1 and {self.settings.max_candidates}")
        if run_id is not None and (
            not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 64
            or any(not (ch.isascii() and (ch.isalnum() or ch in "_-"))
                   for ch in run_id)
        ):
            raise ValueError("run_id must be a non-empty safe identifier")
        workers = self.settings.max_concurrency if max_concurrency is None \
            else max_concurrency
        if isinstance(workers, bool) or not isinstance(workers, int) \
                or not 1 <= workers <= self.settings.max_concurrency:
            raise ValueError(
                f"max_concurrency must be between 1 and "
                f"{self.settings.max_concurrency}")
        now = self._now()
        timestamp = now if as_of is None else as_of
        self._validate_as_of(timestamp, now)
        scan = self.scanner_runs.get(scan_id)
        if scan is None:
            raise KeyError(f"unknown scan_id {scan_id!r}")
        if scan["mode"] != "RESEARCH":
            raise ValueError("candidate research requires a RESEARCH scanner run")
        scan_as_of = scan.get("as_of")
        if not isinstance(scan_as_of, str):
            raise ValueError("scanner run has no point-in-time timestamp")
        scan_timestamp = datetime.fromisoformat(scan_as_of)
        self._validate_aware(scan_timestamp, "scanner as_of")
        if timestamp < scan_timestamp:
            raise ValueError("research as_of cannot precede the scanner run as_of")
        rows = self.scanner_runs.candidates(
            scan_id, accepted_only=True, selected_only=True, limit=limit, offset=0)
        if not rows:
            raise ValueError("scanner run has no selected Top-N candidates")
        candidates = [
            (self.instruments[row["instrument_id"]], float(row["score"]), rank)
            for rank, row in enumerate(rows, start=1)
            if row["instrument_id"] in self.instruments
        ]
        run_id = run_id or str(uuid4())
        snapshots: list[ResearchSnapshot] = []
        failures = [
            f"{row['instrument_id']}:UNKNOWN_INSTRUMENT"
            for row in rows if row["instrument_id"] not in self.instruments
        ]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._research_one, instrument, timestamp, run_id, rank, score
                ): instrument
                for instrument, score, rank in candidates
            }
            for future in as_completed(futures):
                instrument = futures[future]
                try:
                    snapshots.append(future.result())
                except Exception as exc:
                    logger.exception(
                        "candidate research failed",
                        extra={"instrument_id": instrument.instrument_id, "run_id": run_id})
                    failures.append(
                        f"{instrument.instrument_id}:{type(exc).__name__}")
        snapshots.sort(key=lambda item: item.scanner_rank)
        if strategy_signals:
            enriched = []
            for snapshot in snapshots:
                signal = strategy_signals.get(snapshot.instrument_id)
                if signal is None:
                    enriched.append(snapshot)
                    continue
                if signal.instrument_id != snapshot.instrument_id or signal.timestamp > timestamp:
                    raise ValueError("strategy evidence identity or timestamp mismatch")
                record = ResearchEvidenceRecord(
                    evidence_id=str(uuid4()), instrument_id=snapshot.instrument_id,
                    component="strategy", as_of=timestamp, observed_at=signal.timestamp,
                    retrieved_at=now, source=f"strategy:{signal.strategy}",
                    quality=EvidenceQuality.VALID,
                    payload={
                        "signal_id": str(signal.signal_id), "side": signal.side.value,
                        "entry_price": signal.entry_price, "stop_loss": signal.stop_loss,
                        "take_profit": signal.take_profit, "reasons": signal.reasons,
                    },
                    provenance={"kind": "deterministic_strategy", "execution": "NOT_SUBMITTED"},
                )
                enriched.append(replace(
                    snapshot, evidence=(*snapshot.evidence, record),
                    components=(*snapshot.components,
                                ComponentAssessment("strategy", EvidenceStatus.AVAILABLE, 1, record.source)),
                ))
            snapshots = enriched
        status = "FAILED" if not snapshots else ("PARTIAL" if failures or any(
            snapshot.status != "COMPLETE" for snapshot in snapshots) else "COMPLETE")
        run = CandidateResearchRun(
            run_id=run_id,
            scan_id=scan_id,
            as_of=timestamp,
            created_at=now,
            status=status,
            requested_count=len(rows),
            completed_count=len(snapshots),
            failed_count=len(failures),
            snapshot_ids=tuple(snapshot.snapshot_id for snapshot in snapshots),
            failure_summary=tuple(failures),
            configuration_fingerprint=self.configuration_fingerprint,
        )
        self.research_runs.save_run(run, snapshots)
        return run, tuple(snapshots)

    def _research_one(
        self,
        instrument: Instrument,
        as_of: datetime,
        run_id: str,
        scanner_rank: int,
        scanner_score: float,
    ) -> ResearchSnapshot:
        cache_key = (instrument.instrument_id, as_of.isoformat(), self._fingerprint())
        cached = self._cached(cache_key)
        if cached is None:
            evidence, scores, components, warnings = self._collect(instrument, as_of)
            self._store_cache(cache_key, (evidence, scores, components, warnings))
        else:
            evidence, scores, components, warnings = cached
            evidence = tuple(replace(record, evidence_id=str(uuid4())) for record in evidence)
        complete = all(item.status is EvidenceStatus.AVAILABLE for item in components)
        return ResearchSnapshot(
            snapshot_id=str(uuid4()),
            run_id=run_id,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            market=instrument.market,
            scanner_rank=scanner_rank,
            scanner_score=scanner_score,
            as_of=as_of,
            created_at=self._now(),
            status="COMPLETE" if complete and not warnings else "PARTIAL",
            components=components,
            scores=scores,
            evidence=evidence,
            warnings=warnings,
        )

    def _collect(
        self, instrument: Instrument, as_of: datetime
    ) -> tuple[
        tuple[ResearchEvidenceRecord, ...],
        tuple[ResearchEvidence, ...],
        tuple[ComponentAssessment, ...],
        tuple[str, ...],
    ]:
        evidence: list[ResearchEvidenceRecord] = []
        scores: list[ResearchEvidence] = []
        components: list[ComponentAssessment] = []
        warnings: list[str] = []
        start = as_of - timedelta(days=self.settings.lookback_days)
        try:
            bars = self.market_data.get_ohlcv(
                instrument, self.settings.interval, start, as_of)
            report = validate_bars(
                bars, self.settings.interval, timezone=instrument.timezone,
                requested_start=start, requested_end=as_of)
            if not report.ok:
                raise _EvidenceRejected("BAR_QUALITY:" + ",".join(report.issues))
            if len(bars) < self.settings.regime_lookback_bars + 1:
                raise _EvidenceRejected("INSUFFICIENT_HISTORY")
            if bars.index.max().to_pydatetime() > as_of:
                raise _EvidenceRejected("FUTURE_BAR")
            evaluator = MarketRegimeEvaluator(RegimeConfig(
                interval=self.settings.interval,
                lookback_bars=self.settings.regime_lookback_bars,
                max_bar_age=self.settings.regime_max_bar_age,
            ))
            regime = evaluator.evaluate(instrument, bars, as_of=as_of)
            retrieved = self._now()
            close = bars["close"].astype(float)
            volume = bars["volume"].astype(float)
            returns = np.log(close / close.shift(1)).dropna().tail(
                self.settings.regime_lookback_bars)
            if returns.empty or not np.isfinite(returns.to_numpy()).all():
                raise _EvidenceRejected("INVALID_RETURNS")
            volatility = float(returns.std(ddof=0))
            momentum = float(np.clip(
                returns.sum() / max(volatility * sqrt(len(returns)), 1e-12), -1, 1))
            recent_volume = float(volume.tail(5).mean())
            baseline_volume = float(volume.iloc[:-5].tail(20).mean()) if len(volume) > 5 else 0
            relative_volume = (
                recent_volume / baseline_volume if baseline_volume > 0 else None)
            volume_score = 0.0 if relative_volume is None else float(np.clip(
                np.log(max(relative_volume, 1e-12)) / 2.0, -1, 1))
            fast_window = min(20, len(close) - 1)
            slow_window = min(50, len(close))
            sma_fast = float(close.tail(fast_window).mean())
            sma_slow = float(close.tail(slow_window).mean())
            moving_average_score = float(np.clip(
                (sma_fast / sma_slow - 1.0) / 0.05, -1.0, 1.0))
            observed_at = bars.index.max().to_pydatetime()
            for component, score, values in (
                ("momentum", momentum, {
                    "return_over_window": float(close.iloc[-1] / close.iloc[-len(returns) - 1] - 1),
                    "realized_volatility": volatility,
                    "window_bars": len(returns),
                }),
                ("volume", volume_score, {
                    "recent_average_volume": recent_volume,
                    "baseline_average_volume": baseline_volume,
                    "relative_volume": relative_volume,
                }),
            ):
                scores.append(ResearchEvidence(
                    instrument_id=instrument.instrument_id,
                    component=component,
                    score=score,
                    observed_at=observed_at,
                    source=f"market_data:{self.market_data.name}",
                ))
                evidence.append(self._record(
                    instrument, component, as_of, observed_at, retrieved,
                    self.market_data.name, values, {"interval": self.settings.interval}))
            evidence.append(self._record(
                instrument, "technical", as_of, observed_at, retrieved,
                self.market_data.name, {
                    "fast_sma": sma_fast,
                    "fast_window_bars": fast_window,
                    "slow_sma": sma_slow,
                    "slow_window_bars": slow_window,
                    "moving_average_trend_score": moving_average_score,
                }, {"interval": self.settings.interval}))
            components.append(ComponentAssessment(
                "technical", EvidenceStatus.AVAILABLE, 3, self.market_data.name))
            scores.append(ResearchEvidence(
                instrument_id=instrument.instrument_id,
                component="regime",
                score=regime.directional_score,
                observed_at=regime.end_at,
                source=f"market_regime:{self.settings.interval}",
            ))
            evidence.append(self._record(
                instrument, "regime", as_of, regime.end_at, retrieved,
                "market_regime_evaluator", {
                    "label": regime.label.value,
                    "directional_score": regime.directional_score,
                    "volatility": regime.volatility,
                    "interval": regime.interval,
                    "lookback_bars": regime.lookback_bars,
                    "window_start": regime.start_at.isoformat(),
                    "window_end": regime.end_at.isoformat(),
                }, {"algorithm": "MarketRegimeEvaluator"}))
            components.append(ComponentAssessment(
                "regime", EvidenceStatus.AVAILABLE, 1, "MarketRegimeEvaluator"))
        except (_EvidenceRejected, DataQualityError, RegimeUnavailable) as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            components.extend((
                ComponentAssessment("technical", EvidenceStatus.REJECTED, 0,
                                    self.market_data.name, (reason,)),
                ComponentAssessment("regime", EvidenceStatus.REJECTED, 0,
                                    "MarketRegimeEvaluator", (reason,)),
            ))
            warnings.append(f"TECHNICAL_EVIDENCE_REJECTED:{reason}")
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            logger.warning(
                "technical candidate evidence unavailable",
                extra={"instrument_id": instrument.instrument_id, "reason": reason})
            components.extend((
                ComponentAssessment("technical", EvidenceStatus.UNAVAILABLE, 0,
                                    self.market_data.name, (reason,)),
                ComponentAssessment("regime", EvidenceStatus.UNAVAILABLE, 0,
                                    "MarketRegimeEvaluator", (reason,)),
            ))
            warnings.append(f"TECHNICAL_EVIDENCE_UNAVAILABLE:{reason}")

        self._collect_observation(
            instrument, as_of, "fundamental", self.fundamental_provider,
            self.settings.fundamental_max_age, evidence, components, warnings)
        self._collect_observation(
            instrument, as_of, "sector", self.sector_provider,
            self.settings.sector_max_age, evidence, components, warnings)
        self._collect_news(instrument, as_of, evidence, components, warnings)
        for component in ("macro", "sentiment"):
            components.append(ComponentAssessment(
                component, EvidenceStatus.MISSING, 0, reasons=("PROVIDER_NOT_CONFIGURED",)))
        status = tuple(evidence), tuple(scores), tuple(components), tuple(warnings)
        return status

    def _collect_observation(
        self,
        instrument: Instrument,
        as_of: datetime,
        component: Literal["fundamental", "sector"],
        provider: ResearchObservationProvider | None,
        max_age: timedelta,
        evidence: list[ResearchEvidenceRecord],
        components: list[ComponentAssessment],
        warnings: list[str],
    ) -> None:
        if provider is None:
            components.append(ComponentAssessment(
                component, EvidenceStatus.MISSING, 0,
                reasons=("PROVIDER_NOT_CONFIGURED",)))
            return
        source = getattr(provider, "name", type(provider).__name__)
        if self._now() - as_of > self.settings.news_current_window \
                and not getattr(provider, "supports_point_in_time", False):
            reason = "HISTORICAL_ARCHIVE_REQUIRED"
            warnings.append(f"{component.upper()}_UNAVAILABLE:{reason}")
            components.append(ComponentAssessment(
                component, EvidenceStatus.UNAVAILABLE, 0, source, (reason,)))
            return
        try:
            observation = provider.get_observation(
                instrument, component=component, as_of=as_of, max_age=max_age)
            if observation is None:
                components.append(ComponentAssessment(
                    component, EvidenceStatus.MISSING, 0, source,
                    (f"NO_{component.upper()}_OBSERVATION",)))
                return
            if not isinstance(observation, ResearchObservation) \
                    or observation.instrument_id != instrument.instrument_id \
                    or observation.market != instrument.market \
                    or observation.component != component:
                raise _EvidenceRejected("OBSERVATION_SCOPE_MISMATCH")
            age = as_of - observation.observed_at
            if age < timedelta(0) or age > max_age:
                raise _EvidenceRejected("OBSERVATION_FUTURE_OR_STALE")
            evidence.append(self._record(
                instrument, component, as_of, observation.observed_at, self._now(),
                observation.source, {
                    "subject": observation.subject,
                    "content": observation.content,
                    "reference": observation.reference,
                }, {"provider": source}))
            components.append(ComponentAssessment(component, EvidenceStatus.AVAILABLE, 1, source))
        except _EvidenceRejected as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            warnings.append(f"{component.upper()}_REJECTED:{reason}")
            components.append(ComponentAssessment(
                component, EvidenceStatus.REJECTED, 0, source, (reason,)))
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            warnings.append(f"{component.upper()}_UNAVAILABLE:{reason}")
            components.append(ComponentAssessment(
                component, EvidenceStatus.UNAVAILABLE, 0, source, (reason,)))

    def _collect_news(
        self,
        instrument: Instrument,
        as_of: datetime,
        evidence: list[ResearchEvidenceRecord],
        components: list[ComponentAssessment],
        warnings: list[str],
    ) -> None:
        provider = self.news_provider
        if provider is None:
            components.append(ComponentAssessment(
                "news", EvidenceStatus.MISSING, 0,
                reasons=("PROVIDER_NOT_CONFIGURED",)))
            return
        source = getattr(provider, "name", type(provider).__name__)
        if self._now() - as_of > self.settings.news_current_window \
                and not getattr(provider, "supports_point_in_time", False):
            components.append(ComponentAssessment(
                "news", EvidenceStatus.UNAVAILABLE, 0, source,
                ("HISTORICAL_NEWS_ARCHIVE_REQUIRED",)))
            warnings.append("NEWS_UNAVAILABLE:HISTORICAL_NEWS_ARCHIVE_REQUIRED")
            return
        try:
            events = provider.get_news(NewsQuery(
                symbols=(instrument.symbol,),
                affected_market=instrument.market,
                start_time=as_of - self.settings.news_max_age,
                end_time=as_of,
                limit=20,
            ))
            if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) \
                    or len(events) > 20:
                raise _EvidenceRejected("INVALID_NEWS_COLLECTION")
            seen: set[str] = set()
            accepted: list[NewsEvent] = []
            for event in events:
                if not isinstance(event, NewsEvent):
                    raise _EvidenceRejected("INVALID_NEWS_EVENT")
                event_key = str(event.event_id)
                if event_key in seen:
                    raise _EvidenceRejected("DUPLICATE_NEWS_EVENT")
                seen.add(event_key)
                if event.symbol != instrument.symbol \
                        or (event.affected_market is not None
                            and event.affected_market != instrument.market):
                    raise _EvidenceRejected("NEWS_SCOPE_MISMATCH")
                age = as_of - event.timestamp
                if age < timedelta(0) or age > self.settings.news_max_age:
                    warnings.append(f"NEWS_EVENT_OMITTED:{event_key}:FUTURE_OR_STALE")
                    continue
                accepted.append(event)
            retrieved = self._now()
            for event in accepted:
                evidence.append(self._record(
                    instrument, "news", as_of, event.timestamp, retrieved,
                    event.source, {
                        "event_id": str(event.event_id),
                        "headline": event.headline,
                        "event_type": event.event_type.value,
                        "reported_sentiment": event.sentiment.value,
                        "relevance": event.relevance,
                        "reference": event.reference,
                    }, {
                        "provider": source,
                        "provider_event_id": event.provider_event_id,
                        "age_seconds": max(0.0, (as_of - event.timestamp).total_seconds()),
                    }, quality=(
                        EvidenceQuality.STALE
                        if as_of - event.timestamp > timedelta(hours=24)
                        else EvidenceQuality.VALID)))
            components.append(ComponentAssessment(
                "news", EvidenceStatus.AVAILABLE if accepted else EvidenceStatus.MISSING,
                len(accepted), source,
                () if accepted else ("NO_MATCHING_NEWS_EVENTS",)))
        except _EvidenceRejected as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            warnings.append(f"NEWS_REJECTED:{reason}")
            components.append(ComponentAssessment(
                "news", EvidenceStatus.REJECTED, 0, source, (reason,)))
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            warnings.append(f"NEWS_UNAVAILABLE:{reason}")
            components.append(ComponentAssessment(
                "news", EvidenceStatus.UNAVAILABLE, 0, source, (reason,)))

    @staticmethod
    def _record(
        instrument: Instrument,
        component: str,
        as_of: datetime,
        observed_at: datetime | None,
        retrieved_at: datetime,
        source: str,
        payload: Mapping[str, Any],
        provenance: Mapping[str, Any],
        *,
        quality: EvidenceQuality = EvidenceQuality.VALID,
    ) -> ResearchEvidenceRecord:
        return ResearchEvidenceRecord(
            evidence_id=str(uuid4()),
            instrument_id=instrument.instrument_id,
            component=component,
            as_of=as_of,
            observed_at=observed_at,
            retrieved_at=retrieved_at,
            source=source,
            quality=quality,
            payload=payload,
            provenance=provenance,
        )

    def _now(self) -> datetime:
        value = self.clock()
        self._validate_aware(value, "clock")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_aware(value: datetime, name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    def _validate_as_of(self, as_of: datetime, now: datetime) -> None:
        self._validate_aware(as_of, "as_of")
        if as_of > now:
            raise ValueError("as_of must not be in the future")

    def _fingerprint(self) -> str:
        return "|".join((
            self.market_data.name,
            getattr(self.news_provider, "name", ""),
            getattr(self.fundamental_provider, "name", ""),
            getattr(self.sector_provider, "name", ""),
            repr(self.settings),
        ))

    @property
    def configuration_fingerprint(self) -> str:
        return self._fingerprint()

    def _cached(self, key: tuple[str, str, str]) -> tuple[Any, ...] | None:
        if self.settings.cache_ttl_seconds == 0:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            added_at, value = entry
            if monotonic() - added_at > self.settings.cache_ttl_seconds:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    def _store_cache(self, key: tuple[str, str, str], value: tuple[Any, ...]) -> None:
        if self.settings.cache_ttl_seconds == 0:
            return
        with self._cache_lock:
            self._cache[key] = (monotonic(), value)
            self._cache.move_to_end(key)
            while len(self._cache) > self.settings.cache_capacity:
                self._cache.popitem(last=False)
