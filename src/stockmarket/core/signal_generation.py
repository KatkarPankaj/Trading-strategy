"""Deterministic signal generation from persisted strategy-selected opportunities."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from time import perf_counter
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .data.provider import (
    DataProviderError,
    DataQualityError,
    MarketDataProvider,
    interval_delta,
)
from .data.quality import validate_bars
from .market_session import MarketSession
from .markets import MarketRegistry, SessionPhase, UnknownMarket
from .models import Instrument, Signal, SignalSide, TradingStatus
from .strategies.base import Strategy, StrategyMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalGenerationSettings:
    max_opportunity_age: timedelta = timedelta(minutes=5)
    max_market_data_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        for name in ("max_opportunity_age", "max_market_data_age"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
            if value > timedelta(days=7):
                raise ValueError(f"{name} must not exceed seven days")


def parse_signal_generation_settings(raw: str | None) -> SignalGenerationSettings:
    if raw is None or not raw.strip():
        return SignalGenerationSettings()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SIGNAL_GENERATION_SETTINGS must be valid JSON") from exc
    allowed = {"max_opportunity_age_seconds", "max_market_data_age_seconds"}
    if not isinstance(payload, dict) or set(payload).difference(allowed):
        raise ValueError("SIGNAL_GENERATION_SETTINGS contains unsupported fields")
    values: dict[str, timedelta] = {}
    for field_name, setting_name in (
        ("max_opportunity_age", "max_opportunity_age_seconds"),
        ("max_market_data_age", "max_market_data_age_seconds"),
    ):
        if setting_name not in payload:
            continue
        seconds = payload[setting_name]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not isfinite(seconds) or not 0 < seconds <= 604800:
            raise ValueError(f"{setting_name} must be between 0 and 604800 seconds")
        values[field_name] = timedelta(seconds=seconds)
    return SignalGenerationSettings(**values)


class SignalGenerationService:
    """Generate and persist signals; this service has no trading/execution dependencies."""

    def __init__(
        self,
        market_data: MarketDataProvider,
        autonomous_repository: Any,
        research_repository: Any,
        opportunity_repository: Any,
        signal_repository: Any,
        instruments: Mapping[str, Instrument],
        markets: MarketRegistry,
        strategies: Mapping[str, Strategy],
        sessions: Mapping[str, MarketSession],
        *,
        settings: SignalGenerationSettings = SignalGenerationSettings(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not strategies or len(strategies) > 20:
            raise ValueError("strategies must contain between 1 and 20 entries")
        for name, strategy in strategies.items():
            if not isinstance(name, str) or not name.strip() \
                    or not isinstance(strategy, Strategy) or strategy.name != name:
                raise ValueError("strategy registry contains an invalid strategy entry")
        self.market_data = market_data
        self.autonomous_repository = autonomous_repository
        self.research_repository = research_repository
        self.opportunity_repository = opportunity_repository
        self.signal_repository = signal_repository
        self.instruments = dict(instruments)
        self.markets = markets
        self.strategies = dict(strategies)
        self.sessions = {key.upper(): value for key, value in sessions.items()}
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def generate(
        self,
        run_id: str,
        candidate_id: str,
        *,
        evaluation_as_of: datetime,
    ) -> dict[str, Any]:
        started = perf_counter()
        self._require_aware(evaluation_as_of, "evaluation_as_of")
        now = self._now()
        evaluation_as_of = evaluation_as_of.astimezone(timezone.utc)
        if evaluation_as_of > now:
            return self._reject(
                "FUTURE_EVALUATION_TIMESTAMP",
                run_id=run_id,
                candidate_id=candidate_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        run = self.autonomous_repository.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown autonomous research run {run_id!r}")
        run_payload = run.get("payload")
        if not isinstance(run_payload, Mapping):
            return self._reject(
                "INVALID_PERSISTED_RUN",
                run_id=run_id,
                candidate_id=candidate_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        request = run_payload.get("request")
        if not isinstance(request, Mapping) or run.get("status") not in {
                "COMPLETE", "PARTIAL"}:
            return self._reject(
                "INVALID_PERSISTED_RUN",
                run_id=run_id,
                candidate_id=candidate_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        allowed_markets = request.get("markets")
        allowed_asset_classes = request.get("asset_classes")
        allowed_strategies = request.get("strategy_allowlist")
        if not all(isinstance(values, (list, tuple)) for values in (
                allowed_markets, allowed_asset_classes, allowed_strategies)):
            return self._reject(
                "INVALID_PERSISTED_RUN",
                run_id=run_id,
                candidate_id=candidate_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if request.get("mode") != "PAPER":
            return self._reject(
                "NON_PAPER_RESEARCH_RUN",
                run_id=run_id,
                candidate_id=candidate_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        candidate = self.autonomous_repository.get_candidate(run_id, candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate {candidate_id!r} in run {run_id!r}")

        instrument_id = str(candidate["instrument_id"])
        candidate_payload = candidate.get("payload")
        if not isinstance(candidate_payload, Mapping):
            return self._reject(
                "INVALID_PERSISTED_CANDIDATE",
                run_id=run_id,
                candidate_id=instrument_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if candidate["stage"] == "SIGNAL_GENERATED":
            existing = self.signal_repository.latest_for_candidate(
                run_id, instrument_id)
            if existing is not None and datetime.fromisoformat(
                    existing["evaluation_as_of"]) == evaluation_as_of:
                return self._duplicate(existing)
            return self._reject(
                "INVALID_OPPORTUNITY_STATE",
                run_id=run_id,
                candidate_id=instrument_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if run["stage"] != "STRATEGY_SELECTED" \
                or candidate["stage"] != "STRATEGY_SELECTED":
            return self._reject(
                "INVALID_OPPORTUNITY_STATE",
                run_id=run_id,
                candidate_id=instrument_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        opportunity_id = candidate.get("opportunity_id")
        snapshot_id = candidate.get("snapshot_id")
        opportunity_row = (
            self.opportunity_repository.get_opportunity(opportunity_id)
            if opportunity_id else None)
        snapshot_row = (
            self.research_repository.get_snapshot(snapshot_id)
            if snapshot_id else None)
        if opportunity_row is None or snapshot_row is None:
            return self._reject(
                "PERSISTED_OPPORTUNITY_OR_SNAPSHOT_MISSING",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        opportunity = opportunity_row.get("payload")
        snapshot = snapshot_row.get("payload")
        if not isinstance(opportunity, Mapping) or not isinstance(snapshot, Mapping):
            return self._reject(
                "INVALID_PERSISTED_OPPORTUNITY_OR_SNAPSHOT",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        assessment = opportunity.get("assessment") if isinstance(
            opportunity, Mapping) else None
        context = assessment.get("input_context") if isinstance(
            assessment, Mapping) else None
        candidate_opportunity = candidate_payload.get("opportunity")
        if not isinstance(opportunity, Mapping) or not isinstance(
                assessment, Mapping) or not isinstance(context, Mapping) \
                or not isinstance(candidate_opportunity, Mapping) \
                or opportunity_row.get("state") != "STRATEGY_SELECTED" \
                or opportunity.get("state") != "STRATEGY_SELECTED" \
                or candidate_opportunity.get("state") != "STRATEGY_SELECTED":
            return self._reject(
                "INVALID_OPPORTUNITY_STATE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        try:
            opportunity_as_of = self._parse_time(context["as_of"], "opportunity as_of")
            run_as_of = self._parse_time(run["as_of"], "run as_of")
            request_as_of = self._parse_time(request["as_of"], "request as_of")
            snapshot_as_of = self._parse_time(snapshot["as_of"], "snapshot as_of")
            snapshot_created_at = self._parse_time(
                snapshot["created_at"], "snapshot created_at")
            assessed_at = self._parse_time(
                assessment["assessed_at"], "assessment assessed_at")
            opportunity_created_at = self._parse_time(
                opportunity["created_at"], "opportunity created_at")
            snapshot_context_created_at = self._parse_time(
                context["snapshot_created_at"], "snapshot context created_at")
        except (KeyError, TypeError, ValueError) as exc:
            return self._reject(
                "INVALID_OPPORTUNITY_TIMESTAMP",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=str(exc),
            )
        if context.get("snapshot_id") != snapshot_id \
                or opportunity.get("opportunity_id") != opportunity_id \
                or assessment.get("snapshot_id") != snapshot_id \
                or assessment.get("status") != "COMPLETE" \
                or snapshot.get("snapshot_id") != snapshot_id \
                or context.get("instrument_id") != instrument_id \
                or candidate_payload.get("instrument_id") != instrument_id \
                or run_as_of != opportunity_as_of \
                or request_as_of != run_as_of \
                or snapshot_as_of != opportunity_as_of \
                or snapshot_context_created_at != snapshot_created_at \
                or snapshot_row.get("instrument_id") != instrument_id \
                or assessment.get("instrument_id") != instrument_id:
            return self._reject(
                "OPPORTUNITY_IDENTITY_MISMATCH",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if opportunity_as_of > evaluation_as_of or assessed_at > evaluation_as_of \
                or opportunity_created_at > evaluation_as_of \
                or snapshot_created_at > evaluation_as_of:
            return self._reject(
                "FUTURE_OPPORTUNITY_DATA_REJECTED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if evaluation_as_of - opportunity_as_of > self.settings.max_opportunity_age:
            return self._reject(
                "OPPORTUNITY_STALE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        selected_strategy = assessment.get("recommended_strategy")
        strategy_name = selected_strategy if isinstance(selected_strategy, str) else ""
        strategy = self.strategies.get(strategy_name)
        instrument = self.instruments.get(instrument_id)
        if strategy is None:
            return self._reject(
                "STRATEGY_NOT_REGISTERED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name or "unknown",
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        metadata = getattr(strategy, "metadata", None)
        if not isinstance(metadata, StrategyMetadata):
            return self._reject(
                "STRATEGY_METADATA_UNAVAILABLE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        selected_metadata = next(
            (item for item in context.get("registered_strategies", [])
             if isinstance(item, Mapping) and item.get("name") == strategy_name),
            None,
        )
        if selected_metadata is None \
                or selected_metadata.get("implementation") != type(strategy).__name__ \
                or selected_metadata.get("version") != metadata.version:
            return self._reject(
                "STRATEGY_VERSION_MISMATCH",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if instrument is None:
            return self._reject(
                "INSTRUMENT_NOT_REGISTERED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if context.get("symbol") != instrument.symbol \
                or context.get("market") != instrument.market \
                or context.get("asset_class") != instrument.asset_class.value \
                or context.get("currency") != instrument.currency \
                or context.get("timezone") != instrument.timezone \
                or snapshot.get("market") != instrument.market \
                or instrument.market not in allowed_markets \
                or instrument.asset_class.value not in allowed_asset_classes:
            return self._reject(
                "INSTRUMENT_IDENTITY_OR_ALLOWLIST_MISMATCH",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if strategy_name not in allowed_strategies:
            return self._reject(
                "STRATEGY_NOT_ALLOWED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if instrument.asset_class not in metadata.supported_asset_classes \
                or ("*" not in metadata.supported_markets
                    and instrument.market.upper() not in {
                        market.upper() for market in metadata.supported_markets}):
            return self._reject(
                "STRATEGY_INCOMPATIBLE_WITH_INSTRUMENT",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if not instrument.active or not instrument.tradable \
                or instrument.trading_status is not TradingStatus.ACTIVE:
            return self._reject(
                "INSTRUMENT_NOT_ACTIVE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        try:
            market = self.markets.get(instrument.market)
        except UnknownMarket:
            return self._reject(
                "UNKNOWN_MARKET",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        market_date = evaluation_as_of.astimezone(
            ZoneInfo(market.timezone)).date()
        if not market.is_covered(market_date):
            return self._reject(
                "UNKNOWN_CALENDAR_COVERAGE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if not market.calendar.is_trading_day(market_date):
            return self._reject(
                "NON_TRADING_DAY",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if market.phase(evaluation_as_of) is not SessionPhase.REGULAR:
            return self._reject(
                "OUTSIDE_REGULAR_SESSION",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        session = self.sessions.get(instrument.market.upper())
        if session is None:
            return self._reject(
                "MARKET_SESSION_CONFIG_UNAVAILABLE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if session.timezone != instrument.timezone:
            return self._reject(
                "MARKET_SESSION_TIMEZONE_MISMATCH",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        if session.market_open != market.calendar.open_time \
                or session.market_close != market.calendar.close_time:
            return self._reject(
                "MARKET_SESSION_BOUNDARIES_MISMATCH",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        interval = getattr(getattr(strategy, "config", None), "interval", None)
        if not isinstance(interval, str):
            return self._reject(
                "STRATEGY_DATA_REQUIREMENTS_UNAVAILABLE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        volume_window = getattr(
            getattr(strategy, "config", None), "volume_ma_window", 1)
        if isinstance(volume_window, bool) or not isinstance(volume_window, int) \
                or not 1 <= volume_window <= 10000:
            return self._reject(
                "STRATEGY_DATA_REQUIREMENTS_INVALID",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        try:
            step = interval_delta(interval)
        except ValueError:
            return self._reject(
                "STRATEGY_DATA_REQUIREMENTS_INVALID",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        window = max(
            timedelta(days=1),
            step * (volume_window + 2),
        )
        data_start = evaluation_as_of - window
        try:
            bars = self.market_data.get_ohlcv(
                instrument, interval, data_start, evaluation_as_of)
        except DataQualityError as exc:
            return self._reject(
                "MARKET_DATA_INVALID",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=",".join(exc.issues),
            )
        except DataProviderError as exc:
            detail = ",".join(getattr(exc, "issues", ())) or type(exc).__name__
            return self._reject(
                "MARKET_DATA_UNAVAILABLE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=detail,
            )
        except (TypeError, ValueError) as exc:
            return self._reject(
                "MARKET_DATA_INVALID",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                "market data retrieval failed",
                extra={
                    "run_id": run_id,
                    "opportunity_id": opportunity_id,
                    "instrument_id": instrument_id,
                    "provider": str(getattr(self.market_data, "name", "unknown")),
                    "failure_reason": type(exc).__name__,
                },
            )
            return self._reject(
                "MARKET_DATA_PROVIDER_FAILED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=type(exc).__name__,
            )
        report = validate_bars(
            bars,
            interval,
            timezone=instrument.timezone,
            now=evaluation_as_of,
            max_age=self.settings.max_market_data_age,
            requested_start=data_start,
            requested_end=evaluation_as_of,
        )
        if not report.ok:
            issue_codes = set(report.issues)
            reason = (
                "FUTURE_DATA_REJECTED" if "TIMESTAMP_IN_FUTURE" in issue_codes
                else "MARKET_DATA_STALE" if "STALE_BARS" in issue_codes
                else "MARKET_DATA_INVALID"
            )
            return self._reject(
                reason,
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
                detail=",".join(report.issues),
            )
        data_timestamp = bars.index.max().to_pydatetime()
        if data_timestamp > evaluation_as_of:
            return self._reject(
                "FUTURE_DATA_REJECTED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )

        config = getattr(strategy, "config", None)
        try:
            config_mapping = json.loads(json.dumps(
                self._config_mapping(config), default=self._json_default))
        except ValueError:
            return self._reject(
                "STRATEGY_DATA_REQUIREMENTS_UNAVAILABLE",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                started=started,
            )
        provider_name = str(getattr(self.market_data, "name", "unknown"))
        bars_json = bars.to_json(
            orient="split", date_format="iso", double_precision=15)
        input_material = {
            "run_id": run_id,
            "candidate_id": instrument_id,
            "opportunity_id": opportunity_id,
            "snapshot_id": snapshot_id,
            "instrument": {
                "instrument_id": instrument.instrument_id,
                "market": instrument.market,
                "asset_class": instrument.asset_class.value,
                "currency": instrument.currency,
                "timezone": instrument.timezone,
            },
            "strategy": strategy_name,
            "strategy_implementation": type(strategy).__name__,
            "strategy_version": metadata.version,
            "strategy_config": config_mapping,
            "evaluation_as_of": evaluation_as_of.isoformat(),
            "data_timestamp": data_timestamp.isoformat(),
            "provider": provider_name,
            "bars": bars_json,
        }
        input_fingerprint = hashlib.sha256(json.dumps(
            input_material, sort_keys=True, separators=(",", ":"),
            default=self._json_default).encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(json.dumps(
            {
                "opportunity_id": opportunity_id,
                "strategy_version": metadata.version,
                "evaluation_as_of": evaluation_as_of.isoformat(),
                "input_fingerprint": input_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        existing_result = self.signal_repository.get_by_key(idempotency_key)
        if existing_result is not None:
            return self._duplicate(existing_result)

        try:
            strategy_signal = strategy.evaluate(
                instrument, bars.copy(deep=True), session,
                as_of=evaluation_as_of)
        except Exception as exc:
            logger.error(
                "deterministic strategy evaluation failed",
                extra={
                    "run_id": run_id,
                    "opportunity_id": opportunity_id,
                    "instrument_id": instrument_id,
                    "strategy": strategy_name,
                    "strategy_version": metadata.version,
                    "failure_reason": type(exc).__name__,
                },
            )
            return self._reject(
                "STRATEGY_EVALUATION_FAILED",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                data_timestamp=data_timestamp,
                input_fingerprint=input_fingerprint,
                started=started,
                detail=type(exc).__name__,
            )
        if not isinstance(strategy_signal, Signal) \
                or strategy_signal.instrument_id != instrument_id \
                or strategy_signal.symbol != instrument.symbol \
                or strategy_signal.strategy != strategy_name \
                or strategy_signal.timestamp != data_timestamp \
                or any(
                    price is not None and not instrument.is_valid_price(price)
                    for price in (
                        strategy_signal.entry_price,
                        strategy_signal.stop_loss,
                        strategy_signal.take_profit,
                    )
                ):
            return self._reject(
                "INVALID_STRATEGY_OUTPUT",
                run_id=run_id,
                candidate_id=instrument_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=metadata.version,
                evaluation_as_of=evaluation_as_of,
                data_timestamp=data_timestamp,
                input_fingerprint=input_fingerprint,
                started=started,
            )
        signal_id = uuid5(NAMESPACE_URL, f"stockmarket:signal:{idempotency_key}")
        strategy_signal = replace(strategy_signal, signal_id=signal_id)
        result = {
            "generation_id": uuid5(
                NAMESPACE_URL, f"stockmarket:signal-generation:{idempotency_key}").hex,
            "status": "SIGNAL_GENERATED",
            "signal_type": (
                "NO_SIGNAL" if strategy_signal.side is SignalSide.HOLD
                else strategy_signal.side.value
            ),
            "signal": strategy_signal,
            "reason": "; ".join(strategy_signal.reasons) or "STRATEGY_SIGNAL",
            "provenance": {
                "run_id": run_id,
                "candidate_id": instrument_id,
                "opportunity_id": opportunity_id,
                "snapshot_id": snapshot_id,
                "instrument_id": instrument_id,
                "market": instrument.market,
                "asset_class": instrument.asset_class.value,
                "strategy": strategy_name,
                "strategy_implementation": type(strategy).__name__,
                "strategy_version": metadata.version,
                "strategy_config": config_mapping,
                "evaluation_as_of": evaluation_as_of.isoformat(),
                "opportunity_as_of": opportunity_as_of.isoformat(),
                "data_timestamp": data_timestamp.isoformat(),
                "data_range": {
                    "start": bars.index.min().to_pydatetime().isoformat(),
                    "end": data_timestamp.isoformat(),
                    "interval": interval,
                    "bars": len(bars),
                },
                "provider": provider_name,
                "research_only_provider": bool(
                    getattr(self.market_data, "research_only", False)),
                "data_quality": "VALID",
                "input_fingerprint": input_fingerprint,
                "strategy_inputs": {
                    "reasons": strategy_signal.reasons,
                    "invalidation_conditions": strategy_signal.invalidation_conditions,
                    "entry_price": strategy_signal.entry_price,
                    "stop_loss": strategy_signal.stop_loss,
                    "take_profit": strategy_signal.take_profit,
                },
            },
            "generated_at": self._now(),
            "duplicate": False,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
        }
        duration = perf_counter() - started
        logger.info(
            "deterministic signal generated",
            extra={
                "run_id": run_id,
                "opportunity_id": opportunity_id,
                "instrument_id": instrument_id,
                "strategy": strategy_name,
                "strategy_version": metadata.version,
                "signal_type": result["signal_type"],
                "evaluation_timestamp": evaluation_as_of.isoformat(),
                "data_timestamp": data_timestamp.isoformat(),
                "duration_seconds": duration,
            },
        )
        persisted = self.signal_repository.save_result(
            generation_id=result["generation_id"],
            idempotency_key=idempotency_key,
            run_id=run_id,
            candidate_id=instrument_id,
            opportunity_id=opportunity_id,
            snapshot_id=snapshot_id,
            instrument_id=instrument_id,
            strategy_name=strategy_name,
            strategy_version=metadata.version,
            evaluation_as_of=evaluation_as_of,
            data_timestamp=data_timestamp,
            input_fingerprint=input_fingerprint,
            status="SIGNAL_GENERATED",
            reason=result["reason"],
            signal=strategy_signal,
            generated_at=result["generated_at"],
            payload=result,
        )
        return persisted["payload"]

    def _reject(
        self,
        reason: str,
        *,
        run_id: str,
        candidate_id: str,
        evaluation_as_of: datetime,
        started: float,
        opportunity_id: str | None = None,
        snapshot_id: str | None = None,
        instrument_id: str | None = None,
        strategy_name: str = "unknown",
        strategy_version: str = "unknown",
        data_timestamp: datetime | None = None,
        input_fingerprint: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = input_fingerprint or hashlib.sha256(
            f"{reason}:{detail or ''}".encode("utf-8")).hexdigest()
        response = {
            "generation_id": uuid5(
                NAMESPACE_URL,
                f"stockmarket:signal-rejection:{run_id}:{candidate_id}:"
                f"{evaluation_as_of.isoformat()}:{fingerprint}",
            ).hex,
            "status": "REJECTED",
            "signal_type": None,
            "signal": None,
            "reason": reason,
            "detail": detail,
            "provenance": {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "opportunity_id": opportunity_id,
                "snapshot_id": snapshot_id,
                "instrument_id": instrument_id,
                "strategy": strategy_name,
                "strategy_version": strategy_version,
                "evaluation_as_of": evaluation_as_of.isoformat(),
                "data_timestamp": (
                    data_timestamp.isoformat() if data_timestamp else None),
                "input_fingerprint": input_fingerprint,
            },
            "generated_at": self._now(),
            "duplicate": False,
            "execution": "NOT_SUBMITTED",
            "risk_status": "NOT_EVALUATED",
        }
        opportunity_row = (
            self.opportunity_repository.get_opportunity(opportunity_id)
            if opportunity_id else None)
        snapshot_row = (
            self.research_repository.get_snapshot(snapshot_id)
            if snapshot_id else None)
        if opportunity_row is not None and snapshot_row is not None and instrument_id \
                and snapshot_row.get("instrument_id") == instrument_id:
            idempotency_key = hashlib.sha256(json.dumps(
                {
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "opportunity_id": opportunity_id,
                    "strategy_version": strategy_version,
                    "evaluation_as_of": evaluation_as_of.isoformat(),
                    "input_fingerprint": fingerprint,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            result = self.signal_repository.save_result(
                generation_id=response["generation_id"],
                idempotency_key=idempotency_key,
                run_id=run_id,
                candidate_id=candidate_id,
                opportunity_id=opportunity_id,
                snapshot_id=snapshot_id,
                instrument_id=instrument_id,
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                evaluation_as_of=evaluation_as_of,
                data_timestamp=data_timestamp,
                input_fingerprint=fingerprint,
                status="REJECTED",
                reason=reason,
                signal=None,
                generated_at=response["generated_at"],
                payload=response,
            )
            response = result["payload"]
        logger.warning(
            "deterministic signal generation rejected",
            extra={
                "run_id": run_id,
                "opportunity_id": opportunity_id,
                "instrument_id": instrument_id,
                "strategy": strategy_name,
                "strategy_version": strategy_version,
                "evaluation_timestamp": evaluation_as_of.isoformat(),
                "data_timestamp": (
                    data_timestamp.isoformat() if data_timestamp else None),
                "failure_reason": reason,
                "duration_seconds": perf_counter() - started,
            },
        )
        return response

    @staticmethod
    def _duplicate(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row["payload"])
        result["duplicate"] = True
        return result

    def _now(self) -> datetime:
        now = self.clock()
        self._require_aware(now, "clock")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @classmethod
    def _parse_time(cls, value: Any, name: str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{name} is invalid") from exc
        cls._require_aware(value, name)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _config_mapping(config: Any) -> Mapping[str, Any]:
        if config is None:
            return {}
        if is_dataclass(config) and not isinstance(config, type):
            return asdict(config)
        if isinstance(config, Mapping):
            return dict(config)
        raise ValueError("strategy config must be a dataclass or mapping")

    @staticmethod
    def _json_default(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, (datetime, timedelta)):
            return value.isoformat() if isinstance(value, datetime) else value.total_seconds()
        if isinstance(value, frozenset):
            return sorted(value, key=str)
        raise TypeError(f"cannot serialize {type(value).__name__}")
