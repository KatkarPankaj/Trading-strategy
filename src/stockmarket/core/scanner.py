"""Market-aware instrument universe scanning; deliberately has no execution dependencies."""

from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from .data.provider import (
    DataProviderError,
    DataQualityError,
    MarketDataProvider,
    Quote,
    interval_delta,
)
from .data.quality import validate_bars, validate_quote
from .markets import MarketDefinition, MarketRegistry, SessionPhase, UnknownMarket
from .models import AssetClass, Instrument, TradingStatus

logger = logging.getLogger(__name__)


class ScanMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"


class ScanStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class MarketSessionStatus(str, Enum):
    OPEN = "OPEN"
    PRE_OPEN = "PRE_OPEN"
    POST_CLOSE = "POST_CLOSE"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"


class CandidateQuality(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class UnknownUniverse(KeyError):
    pass


class ScanPersistenceError(RuntimeError):
    pass


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _finite_nonnegative(value: float | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    universe_id: str
    name: str
    markets: tuple[str, ...]
    asset_classes: tuple[AssetClass, ...] = (AssetClass.EQUITY, AssetClass.ETF)
    instrument_ids: tuple[str, ...] = ()
    excluded_instrument_ids: tuple[str, ...] = ()
    minimum_price: float | None = None
    maximum_price: float | None = None
    minimum_average_volume: float | None = None
    minimum_turnover_by_currency: Mapping[str, float] = field(default_factory=dict)
    minimum_volatility: float | None = None
    maximum_volatility: float | None = None
    sectors: tuple[str, ...] = ()
    minimum_market_cap_by_currency: Mapping[str, float] = field(default_factory=dict)
    maximum_market_cap_by_currency: Mapping[str, float] = field(default_factory=dict)
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.universe_id, str) or not self.universe_id.strip() \
                or not isinstance(self.name, str) or not self.name.strip() or not self.markets:
            raise ValueError("universe_id, name, and at least one market are required")
        if len(self.universe_id) > 64 or len(self.name) > 200:
            raise ValueError("universe_id and name exceed their length limits")
        if not isinstance(self.markets, tuple) or not isinstance(self.asset_classes, tuple):
            raise ValueError("markets and asset_classes must be tuples")
        if any(not isinstance(market, str) or not market.strip() for market in self.markets):
            raise ValueError("markets must contain non-empty market codes")
        if not self.asset_classes or any(not isinstance(c, AssetClass) for c in self.asset_classes):
            raise ValueError("asset_classes must contain supported AssetClass values")
        for name in ("instrument_ids", "excluded_instrument_ids", "sectors"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{name} must be a tuple of non-empty strings")
        if len(self.markets) > 20 or len(self.instrument_ids) > 100_000 \
                or len(self.excluded_instrument_ids) > 100_000 or len(self.sectors) > 100:
            raise ValueError("universe definition exceeds configured size limits")
        for name in ("minimum_price", "maximum_price"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        for name in ("minimum_average_volume", "minimum_volatility", "maximum_volatility"):
            _finite_nonnegative(getattr(self, name), name)
        if self.minimum_price is not None and self.maximum_price is not None \
                and self.minimum_price > self.maximum_price:
            raise ValueError("minimum_price cannot exceed maximum_price")
        if self.minimum_volatility is not None and self.maximum_volatility is not None \
                and self.minimum_volatility > self.maximum_volatility:
            raise ValueError("minimum_volatility cannot exceed maximum_volatility")
        if not isinstance(self.active, bool):
            raise TypeError("active must be a bool")
        if not isinstance(self.minimum_turnover_by_currency, Mapping):
            raise TypeError("minimum_turnover_by_currency must be a mapping")
        for currency, value in self.minimum_turnover_by_currency.items():
            if not isinstance(currency, str):
                raise ValueError("turnover thresholds must use 3-letter currency codes")
            if len(currency) != 3 or not currency.isascii() or not currency.isalpha() \
                    or currency != currency.upper():
                raise ValueError("turnover thresholds must use 3-letter currency codes")
            _finite_nonnegative(value, f"minimum_turnover_by_currency[{currency}]")
        for mapping_name in (
            "minimum_market_cap_by_currency", "maximum_market_cap_by_currency",
        ):
            mapping = getattr(self, mapping_name)
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{mapping_name} must be a mapping")
            for currency, value in mapping.items():
                if not isinstance(currency, str) or len(currency) != 3 \
                        or not currency.isascii() or not currency.isalpha() \
                        or currency != currency.upper():
                    raise ValueError("market-cap thresholds must use 3-letter currency codes")
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{mapping_name}[{currency}] must be finite and positive")
        for currency in (
            set(self.minimum_market_cap_by_currency)
            & set(self.maximum_market_cap_by_currency)
        ):
            if self.minimum_market_cap_by_currency[currency] \
                    > self.maximum_market_cap_by_currency[currency]:
                raise ValueError(
                    f"minimum market cap exceeds maximum for {currency}")


class InstrumentUniverseProvider(Protocol):
    def list_universes(self) -> tuple[UniverseDefinition, ...]: ...

    def get_universe(self, universe_id: str) -> tuple[UniverseDefinition, tuple[Instrument, ...]]: ...


class StaticUniverseProvider:
    """Validated, data-only universe definitions backed by the instrument registry."""

    def __init__(
        self,
        definitions: Sequence[UniverseDefinition],
        instruments: Mapping[str, Instrument],
    ) -> None:
        self._definitions: dict[str, UniverseDefinition] = {}
        self._instruments = dict(instruments)
        for definition in definitions:
            missing = set(definition.instrument_ids) - set(self._instruments)
            if missing:
                raise ValueError(
                    f"universe {definition.universe_id!r} references unknown instrument "
                    f"{sorted(missing)[0]!r}")
            self.register(definition)

    def register(self, definition: UniverseDefinition) -> None:
        key = definition.universe_id.upper()
        if key in self._definitions:
            raise ValueError(f"universe {key!r} is already registered")
        self._definitions[key] = definition

    def list_universes(self) -> tuple[UniverseDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def get_universe(self, universe_id: str) -> tuple[UniverseDefinition, tuple[Instrument, ...]]:
        try:
            definition = self._definitions[universe_id.upper()]
        except KeyError:
            raise UnknownUniverse(universe_id) from None
        if not definition.active:
            return definition, ()
        included = set(definition.instrument_ids)
        excluded = set(definition.excluded_instrument_ids)
        selected = tuple(sorted(
            (instrument for instrument in self._instruments.values()
             if instrument.market.upper() in {m.upper() for m in definition.markets}
             and instrument.asset_class in definition.asset_classes
             and (not included or instrument.instrument_id in included)
             and instrument.instrument_id not in excluded),
            key=lambda instrument: instrument.instrument_id,
        ))
        return definition, selected


class RegistryUniverseProvider(StaticUniverseProvider):
    """Initial universe source: active-market instruments already in InstrumentRepository."""

    def __init__(
        self,
        instruments: Mapping[str, Instrument],
        market_codes: Sequence[str],
    ) -> None:
        definitions = tuple(
            UniverseDefinition(
                universe_id=f"{market.upper()}_ALL",
                name=f"All registered {market.upper()} instruments",
                markets=(market.upper(),),
            )
            for market in sorted(set(market_codes))
        )
        super().__init__(definitions, instruments)


def parse_universe_definitions(raw_json: str) -> tuple[UniverseDefinition, ...]:
    """Parse SCANNER_UNIVERSES JSON; expressions and unknown properties are refused."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("SCANNER_UNIVERSES must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("SCANNER_UNIVERSES must be a JSON array")
    allowed = set(UniverseDefinition.__dataclass_fields__)
    definitions = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError(f"SCANNER_UNIVERSES entry {index} has unsupported fields")
        item = dict(row)
        classes = item.get("asset_classes", ["EQUITY", "ETF"])
        try:
            if not isinstance(classes, list) or any(not isinstance(value, str) for value in classes):
                raise ValueError("asset_classes must be an array of strings")
            item["asset_classes"] = tuple(AssetClass(value) for value in classes)
            for key in (
                "markets", "instrument_ids", "excluded_instrument_ids", "sectors",
            ):
                if key in item:
                    if not isinstance(item[key], list) or any(
                        not isinstance(value, str) for value in item[key]
                    ):
                        raise ValueError(f"{key} must be an array of strings")
                    item[key] = tuple(item[key])
            if not isinstance(item.get("minimum_turnover_by_currency", {}), dict):
                raise ValueError("minimum_turnover_by_currency must be an object")
            definitions.append(UniverseDefinition(**item))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid SCANNER_UNIVERSES entry {index}: {exc}") from exc
    if len(definitions) > 1000:
        raise ValueError("SCANNER_UNIVERSES cannot define more than 1000 universes")
    return tuple(definitions)


@dataclass(frozen=True, slots=True)
class ScannerSettings:
    max_concurrency: int = 8
    batch_size: int = 100
    top_n: int = 50
    minimum_history_bars: int = 20
    lookback_days: int = 90
    interval: str = "1d"
    maximum_quote_age: timedelta = timedelta(seconds=60)
    volatility_target: float = 0.02
    liquidity_weight: float = 0.35
    momentum_weight: float = 0.25
    volume_expansion_weight: float = 0.15
    volatility_weight: float = 0.15
    quality_weight: float = 0.1

    def __post_init__(self) -> None:
        for name in ("max_concurrency", "batch_size", "top_n", "minimum_history_bars", "lookback_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_concurrency > 128 or self.batch_size > 5000 \
                or self.top_n > 1000 or self.minimum_history_bars > 10000 \
                or self.lookback_days > 3650:
            raise ValueError("scanner settings exceed safe resource limits")
        if self.interval not in ("1d", "1h", "30m", "15m", "5m", "1m"):
            raise ValueError("unsupported scanner interval")
        if not isinstance(self.maximum_quote_age, timedelta) \
                or self.maximum_quote_age <= timedelta(0):
            raise ValueError("maximum_quote_age must be positive")
        weights = (
            self.liquidity_weight, self.momentum_weight,
            self.volume_expansion_weight, self.volatility_weight, self.quality_weight,
        )
        if any(
            isinstance(w, bool) or not isinstance(w, (int, float))
            or not math.isfinite(w) or w < 0
            for w in weights
        ) or sum(weights) <= 0:
            raise ValueError("ranking weights must be finite, non-negative, and not all zero")
        if isinstance(self.volatility_target, bool) \
                or not isinstance(self.volatility_target, (int, float)) \
                or not math.isfinite(self.volatility_target) or self.volatility_target <= 0:
            raise ValueError("volatility_target must be finite and positive")


def parse_scanner_settings(raw_json: str | None) -> ScannerSettings:
    if not raw_json:
        return ScannerSettings()
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("SCANNER_SETTINGS must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("SCANNER_SETTINGS must be a JSON object")
    allowed = (set(ScannerSettings.__dataclass_fields__) - {"maximum_quote_age"}) \
        | {"maximum_quote_age_seconds"}
    if set(payload) - allowed:
        raise ValueError("SCANNER_SETTINGS contains unsupported fields")
    values = dict(payload)
    if "maximum_quote_age_seconds" in values:
        seconds = values.pop("maximum_quote_age_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("maximum_quote_age_seconds must be finite and positive")
        values["maximum_quote_age"] = timedelta(seconds=seconds)
    return ScannerSettings(**values)


@dataclass(frozen=True, slots=True)
class FilterResult:
    name: str
    passed: bool
    reason: str | None = None
    observed: str | float | None = None
    threshold: str | float | None = None


@dataclass(frozen=True, slots=True)
class ScannerCandidate:
    instrument_id: str
    symbol: str
    market: str
    asset_class: AssetClass
    currency: str
    reference_price: float | None
    volume: float | None
    average_volume: float | None
    turnover: float | None
    volatility: float | None
    momentum: float | None
    volume_expansion: float | None
    market_status: MarketSessionStatus
    data_quality: CandidateQuality
    data_timestamp: datetime | None
    data_age_seconds: float | None
    provider: str | None
    provider_status: str
    history_length: int
    filter_results: tuple[FilterResult, ...]
    preliminary_score: float
    score_components: tuple[tuple[str, float], ...]
    rejection_reasons: tuple[str, ...]
    evaluation_failed: bool = False


@dataclass(frozen=True, slots=True)
class ScanResult:
    scan_id: str
    universe_id: str
    markets: tuple[str, ...]
    mode: ScanMode
    started_at: datetime
    completed_at: datetime
    status: ScanStatus
    requested_count: int
    evaluated_count: int
    accepted_count: int
    rejected_count: int
    failed_count: int
    candidates: tuple[ScannerCandidate, ...]
    failure_summary: tuple[str, ...] = ()
    as_of: datetime | None = None


class ScanResultRepository(Protocol):
    def save_scan(
        self,
        result: ScanResult,
        *,
        candidates: Sequence[ScannerCandidate],
    ) -> None: ...


def _market_status(markets: MarketRegistry, instrument: Instrument, at: datetime) -> MarketSessionStatus:
    try:
        market = markets.get(instrument.market)
    except UnknownMarket:
        return MarketSessionStatus.UNKNOWN
    local = at.astimezone(ZoneInfo(market.timezone))
    if not market.is_covered(local.date()):
        return MarketSessionStatus.UNKNOWN
    if not market.calendar.is_trading_day(local.date()):
        return MarketSessionStatus.HOLIDAY
    phase = market.phase(at)
    if phase is SessionPhase.REGULAR:
        return MarketSessionStatus.OPEN
    if phase is SessionPhase.PRE_MARKET:
        return MarketSessionStatus.PRE_OPEN
    if phase is SessionPhase.POST_MARKET:
        return MarketSessionStatus.POST_CLOSE
    return MarketSessionStatus.CLOSED


class MarketScanner:
    """Cheap deterministic scanner. It discovers candidates and never creates or approves orders."""

    def __init__(
        self,
        market_data: MarketDataProvider,
        universes: InstrumentUniverseProvider,
        markets: MarketRegistry,
        *,
        repository: ScanResultRepository | None = None,
        settings: ScannerSettings = ScannerSettings(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._data = market_data
        self._universes = universes
        self._markets = markets
        self._repository = repository
        self._settings = settings
        self._clock = clock

    def list_universes(self) -> tuple[UniverseDefinition, ...]:
        return self._universes.list_universes()

    def get_universe(self, universe_id: str) -> tuple[UniverseDefinition, tuple[Instrument, ...]]:
        return self._universes.get_universe(universe_id)

    def scan(
        self,
        universe_id: str,
        mode: ScanMode | str,
        *,
        top_n: int | None = None,
        as_of: datetime | None = None,
    ) -> ScanResult:
        try:
            selected_mode = ScanMode(mode)
        except ValueError as exc:
            raise ValueError("mode must be RESEARCH or PAPER; LIVE is not supported") from exc
        now = self._clock()
        _aware(now, "clock")
        scan_at = as_of or now
        _aware(scan_at, "as_of")
        if scan_at > now:
            raise ValueError("as_of cannot be in the future")
        if selected_mode is ScanMode.PAPER and scan_at < now - self._settings.maximum_quote_age:
            raise ValueError("PAPER scans require a current as_of timestamp")
        limit = self._settings.top_n if top_n is None else top_n
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("top_n must be between 1 and 1000")
        definition, instruments = self._universes.get_universe(universe_id)
        scan_id = str(uuid4())
        started = now
        logger.info(
            "market scan started",
            extra={"scan_id": scan_id, "universe_id": definition.universe_id,
                   "mode": selected_mode.value, "requested_count": len(instruments)},
        )
        outcomes: list[tuple[ScannerCandidate, bool]] = []
        with ThreadPoolExecutor(max_workers=self._settings.max_concurrency) as pool:
            for offset in range(0, len(instruments), self._settings.batch_size):
                batch = instruments[offset:offset + self._settings.batch_size]
                outcomes.extend(pool.map(
                    lambda instrument: self._evaluate(instrument, definition, selected_mode, scan_at),
                    batch,
                ))
        candidates = [candidate for candidate, _ in outcomes]
        accepted = [c for c in candidates if c.rejection_reasons == () and not c.evaluation_failed]
        candidates = self._rank(candidates, accepted)
        accepted_ids = {c.instrument_id for c in accepted}
        accepted = [c for c in candidates if c.instrument_id in accepted_ids]
        accepted.sort(key=lambda candidate: (-candidate.preliminary_score, candidate.instrument_id))
        failed_count = sum(1 for _, failed in outcomes if failed)
        evaluated_count = len(instruments) - failed_count
        status = (
            ScanStatus.FAILED if not instruments or (evaluated_count == 0 and failed_count)
            else ScanStatus.PARTIAL if failed_count
            else ScanStatus.COMPLETE
        )
        completed = self._clock()
        result = ScanResult(
            scan_id=scan_id, universe_id=definition.universe_id,
            markets=definition.markets, mode=selected_mode,
            started_at=started, completed_at=completed, status=status,
            requested_count=len(instruments), evaluated_count=evaluated_count,
            accepted_count=len(accepted), rejected_count=len(instruments) - len(accepted),
            failed_count=failed_count,
            candidates=tuple(accepted[:limit]),
            as_of=scan_at,
            failure_summary=(
                ("EMPTY_UNIVERSE",) if not instruments else tuple(
                    f"{c.instrument_id}:{c.provider_status}"
                    for c, failed in outcomes if failed
                )[:100]
            ),
        )
        if self._repository is not None:
            try:
                self._repository.save_scan(result, candidates=candidates)
            except Exception as exc:
                raise ScanPersistenceError(
                    f"failed to persist scan {scan_id}: {type(exc).__name__}") from exc
        logger.info(
            "market scan completed",
            extra={"scan_id": scan_id, "universe_id": definition.universe_id,
                   "status": result.status.value, "evaluated_count": evaluated_count,
                   "accepted_count": result.accepted_count, "failed_count": failed_count,
                   "duration_seconds": (completed - started).total_seconds()},
        )
        return result

    def _evaluate(
        self,
        instrument: Instrument,
        universe: UniverseDefinition,
        mode: ScanMode,
        as_of: datetime,
    ) -> tuple[ScannerCandidate, bool]:
        session_status = _market_status(self._markets, instrument, as_of)
        filters: list[FilterResult] = []
        if not instrument.active:
            filters.append(FilterResult("active", False, "instrument is inactive"))
        else:
            filters.append(FilterResult("active", True))
        if instrument.trading_status is not TradingStatus.ACTIVE:
            filters.append(FilterResult(
                "trading_status", False,
                f"instrument status is {instrument.trading_status.value}",
            ))
        else:
            filters.append(FilterResult("trading_status", True))
        if not instrument.tradable:
            filters.append(FilterResult("tradable", False, "instrument is not tradable"))
        else:
            filters.append(FilterResult("tradable", True))
        if instrument.asset_class not in universe.asset_classes:
            filters.append(FilterResult("asset_class", False, "asset class is excluded"))
        else:
            filters.append(FilterResult("asset_class", True))
        market_pass = session_status is not MarketSessionStatus.UNKNOWN
        if mode is ScanMode.PAPER:
            market_pass = market_pass and session_status in (
                MarketSessionStatus.OPEN,
                MarketSessionStatus.PRE_OPEN,
                MarketSessionStatus.POST_CLOSE,
            )
        filters.append(FilterResult(
            "market", market_pass,
            None if market_pass else ("market must be in a configured session in PAPER mode"
                                      if mode is ScanMode.PAPER
                                      else "market session is unknown"),
            session_status.value,
            "OPEN,PRE_OPEN,POST_CLOSE" if mode is ScanMode.PAPER else "known",
        ))
        if any(not item.passed for item in filters[:4]) or not market_pass:
            return self._candidate(
                instrument, session_status, filters,
                quality=CandidateQuality.UNKNOWN, provider_status="NOT_FETCHED",
                price=None, history_length=0, volume=None, average_volume=None,
                turnover=None, volatility=None, momentum=None, volume_expansion=None,
                data_timestamp=None, data_age=None, reasons=(),
            ), False
        try:
            market = self._markets.get(instrument.market)
            start = as_of - timedelta(days=self._settings.lookback_days)
            bars = self._data.get_ohlcv(instrument, self._settings.interval, start, as_of)
            if not isinstance(bars, pd.DataFrame):
                raise DataQualityError(("INVALID_BARS",))
            quality = validate_bars(
                bars, self._settings.interval, timezone=instrument.timezone,
                requested_start=start, requested_end=as_of,
            )
            if not quality.ok:
                quality_status = self._quality_from_issues(quality.issues)
                return self._candidate(
                    instrument, session_status, filters,
                    quality=quality_status, provider_status="DATA_QUALITY_ERROR",
                    price=None, history_length=len(bars), volume=None, average_volume=None,
                    turnover=None, volatility=None, momentum=None, volume_expansion=None,
                    data_timestamp=self._last_bar_timestamp(bars),
                    data_age=None, reasons=quality.issues,
                ), True
            bars = self._completed_bars(bars, as_of, market)
            if len(bars) < self._settings.minimum_history_bars:
                filters.append(FilterResult(
                    "history", False, "insufficient completed bars", float(len(bars))))
                return self._candidate(
                    instrument, session_status, filters,
                    quality=CandidateQuality.INCOMPLETE,
                    provider_status="INSUFFICIENT_HISTORY",
                    price=None, history_length=len(bars), volume=None, average_volume=None,
                    turnover=None, volatility=None, momentum=None, volume_expansion=None,
                    data_timestamp=self._last_bar_timestamp(bars),
                    data_age=None, reasons=("INSUFFICIENT_HISTORY",),
                ), True

            quote: Quote | None = None
            if mode is ScanMode.PAPER:
                quote = self._data.get_quote(instrument)
                quote_report = validate_quote(quote, as_of, self._settings.maximum_quote_age)
                if not quote_report.ok or quote.instrument_id != instrument.instrument_id \
                        or quote.provider != self._data.name \
                        or not instrument.is_valid_price(quote.price):
                    issues = quote_report.issues or ("QUOTE_ID_PROVIDER_OR_TICK_MISMATCH",)
                    timestamp = quote.timestamp if isinstance(quote, Quote) else None
                    age = (
                        max(0.0, (as_of - timestamp).total_seconds())
                        if timestamp is not None and timestamp.tzinfo is not None
                        and timestamp.utcoffset() is not None else None
                    )
                    return self._candidate(
                        instrument, session_status, filters,
                        quality=self._quality_from_issues(issues),
                        provider_status="DATA_QUALITY_ERROR",
                        price=None, history_length=len(bars), volume=None,
                        average_volume=None, turnover=None, volatility=None,
                        momentum=None, volume_expansion=None, data_timestamp=timestamp,
                        data_age=age, reasons=issues,
                    ), True
            reference_price = float(quote.price if quote else bars["close"].iloc[-1])
            volumes = bars["volume"].astype(float)
            average_volume = float(volumes.tail(self._settings.minimum_history_bars).mean())
            volume = float(volumes.iloc[-1])
            turnover = reference_price * average_volume
            close = bars["close"].astype(float)
            returns = close.pct_change().dropna()
            volatility = float(returns.tail(self._settings.minimum_history_bars).std(ddof=0))
            recent_closes = close.tail(self._settings.minimum_history_bars)
            momentum = float(recent_closes.iloc[-1] / recent_closes.iloc[0] - 1.0)
            previous_mean = float(volumes.iloc[:-1].tail(self._settings.minimum_history_bars - 1).mean())
            expansion = volume / previous_mean if previous_mean > 0 else None
            data_timestamp = quote.timestamp if quote else bars.index[-1].to_pydatetime()
            data_age = max(0.0, (as_of - data_timestamp).total_seconds())
            filters.extend(self._metric_filters(
                instrument, universe, reference_price, average_volume,
                turnover, volatility,
            ))
            filters.append(FilterResult("data_quality", True, observed=CandidateQuality.VALID.value))
            candidate = self._candidate(
                instrument, session_status, filters,
                quality=CandidateQuality.VALID, provider_status="OK",
                price=reference_price, history_length=len(bars), volume=volume,
                average_volume=average_volume, turnover=turnover, volatility=volatility,
                momentum=momentum, volume_expansion=expansion,
                data_timestamp=data_timestamp, data_age=data_age, reasons=(),
            )
            return candidate, False
        except DataQualityError as exc:
            return self._candidate(
                instrument, session_status, filters,
                quality=self._quality_from_issues(exc.issues),
                provider_status="DATA_QUALITY_ERROR",
                price=None, history_length=0, volume=None, average_volume=None,
                turnover=None, volatility=None, momentum=None, volume_expansion=None,
                data_timestamp=None, data_age=None, reasons=exc.issues,
            ), True
        except DataProviderError as exc:
            logger.warning(
                "market scan provider failure",
                extra={"instrument_id": instrument.instrument_id,
                       "provider": self._data.name, "error_type": type(exc).__name__},
            )
            return self._candidate(
                instrument, session_status, filters,
                quality=CandidateQuality.UNKNOWN, provider_status=type(exc).__name__,
                price=None, history_length=0, volume=None, average_volume=None,
                turnover=None, volatility=None, momentum=None, volume_expansion=None,
                data_timestamp=None, data_age=None, reasons=(type(exc).__name__,),
            ), True
        except Exception as exc:
            logger.exception(
                "unexpected market scan instrument failure",
                extra={"instrument_id": instrument.instrument_id,
                       "provider": self._data.name},
            )
            return self._candidate(
                instrument, session_status, filters,
                quality=CandidateQuality.UNKNOWN,
                provider_status=f"PROVIDER_ERROR:{type(exc).__name__}",
                price=None, history_length=0, volume=None, average_volume=None,
                turnover=None, volatility=None, momentum=None, volume_expansion=None,
                data_timestamp=None, data_age=None,
                reasons=(f"PROVIDER_ERROR:{type(exc).__name__}",),
            ), True

    def _completed_bars(
        self, bars: pd.DataFrame, as_of: datetime, market: MarketDefinition,
    ) -> pd.DataFrame:
        zone = ZoneInfo(market.timezone)
        cutoff = as_of.astimezone(zone)
        local = bars.index.tz_convert(zone)
        completed = []
        for timestamp in local:
            if self._settings.interval == "1d":
                close_at = datetime.combine(
                    timestamp.date(), market.calendar.close_time_on(timestamp.date()),
                    tzinfo=zone,
                )
                completed.append(cutoff >= close_at and timestamp <= cutoff)
            else:
                interval_end = timestamp.to_pydatetime() + interval_delta(
                    self._settings.interval)
                completed.append(interval_end <= cutoff)
        return bars.loc[completed]

    @staticmethod
    def _last_bar_timestamp(bars: pd.DataFrame) -> datetime | None:
        if not isinstance(bars.index, pd.DatetimeIndex) or not len(bars) or bars.index.hasnans:
            return None
        value = bars.index.max().to_pydatetime()
        return value if value.tzinfo is not None and value.utcoffset() is not None else None

    @staticmethod
    def _quality_from_issues(issues: Sequence[str]) -> CandidateQuality:
        joined = " ".join(issues).upper()
        if "STALE" in joined or "FUTURE" in joined:
            return CandidateQuality.STALE
        if "MISSING" in joined or "INCOMPLETE" in joined or "NO_DATA" in joined:
            return CandidateQuality.INCOMPLETE
        if "INVALID" in joined or "NON_" in joined or "MISMATCH" in joined:
            return CandidateQuality.INVALID
        return CandidateQuality.UNKNOWN

    @staticmethod
    def _metric_filters(
        instrument: Instrument,
        universe: UniverseDefinition,
        price: float,
        average_volume: float,
        turnover: float,
        volatility: float,
    ) -> list[FilterResult]:
        checks = []
        price_ok = (universe.minimum_price is None or price >= universe.minimum_price) \
            and (universe.maximum_price is None or price <= universe.maximum_price)
        checks.append(FilterResult(
            "price", price_ok,
            None if price_ok else "price outside configured bounds", price,
            f"min={universe.minimum_price},max={universe.maximum_price}",
        ))
        volume_ok = universe.minimum_average_volume is None \
            or average_volume >= universe.minimum_average_volume
        checks.append(FilterResult(
            "average_volume", volume_ok,
            None if volume_ok else "average volume below threshold", average_volume,
            universe.minimum_average_volume,
        ))
        minimum_turnover = universe.minimum_turnover_by_currency.get(instrument.currency)
        turnover_ok = minimum_turnover is None or turnover >= minimum_turnover
        checks.append(FilterResult(
            "turnover", turnover_ok,
            None if turnover_ok else f"turnover below {instrument.currency} threshold",
            turnover, minimum_turnover,
        ))
        volatility_ok = (universe.minimum_volatility is None
                         or volatility >= universe.minimum_volatility) \
            and (universe.maximum_volatility is None
                 or volatility <= universe.maximum_volatility)
        checks.append(FilterResult(
            "volatility", volatility_ok,
            None if volatility_ok else "volatility outside configured bounds", volatility,
            f"min={universe.minimum_volatility},max={universe.maximum_volatility}",
        ))
        if universe.sectors:
            sector_ok = instrument.sector is not None \
                and instrument.sector.casefold() in {sector.casefold() for sector in universe.sectors}
            checks.append(FilterResult(
                "sector", sector_ok,
                None if sector_ok else "sector missing or excluded", instrument.sector,
                ",".join(universe.sectors),
            ))
        if universe.minimum_market_cap_by_currency or universe.maximum_market_cap_by_currency:
            cap = instrument.market_cap
            minimum_cap = universe.minimum_market_cap_by_currency.get(instrument.currency)
            maximum_cap = universe.maximum_market_cap_by_currency.get(instrument.currency)
            configured_for_currency = minimum_cap is not None or maximum_cap is not None
            cap_ok = configured_for_currency and cap is not None \
                and (minimum_cap is None or cap >= minimum_cap) \
                and (maximum_cap is None or cap <= maximum_cap)
            checks.append(FilterResult(
                "market_cap", cap_ok,
                None if cap_ok else
                "market cap or currency-specific bounds missing/outside configured bounds",
                cap,
                f"{instrument.currency}:min={minimum_cap},max={maximum_cap}",
            ))
        return checks

    def _candidate(
        self,
        instrument: Instrument,
        session_status: MarketSessionStatus,
        filters: Sequence[FilterResult],
        quality: CandidateQuality,
        provider_status: str,
        price: float | None,
        history_length: int,
        volume: float | None,
        average_volume: float | None,
        turnover: float | None,
        volatility: float | None,
        momentum: float | None,
        volume_expansion: float | None,
        data_timestamp: datetime | None,
        data_age: float | None,
        reasons: Sequence[str] | None,
    ) -> ScannerCandidate:
        evaluated_filters = list(filters)
        if not any(item.name == "data_quality" for item in evaluated_filters):
            passed_quality = quality is CandidateQuality.VALID
            evaluated_filters.append(FilterResult(
                "data_quality", passed_quality,
                None if passed_quality else f"data quality is {quality.value}",
                quality.value,
            ))
        all_reasons = tuple(
            item.reason or item.name for item in evaluated_filters if not item.passed)
        if reasons:
            all_reasons += tuple(reason for reason in reasons if reason not in all_reasons)
        return ScannerCandidate(
            instrument_id=instrument.instrument_id, symbol=instrument.symbol,
            market=instrument.market, asset_class=instrument.asset_class,
            currency=instrument.currency, reference_price=price, volume=volume,
            average_volume=average_volume, turnover=turnover, volatility=volatility,
            momentum=momentum, volume_expansion=volume_expansion,
            market_status=session_status, data_quality=quality,
            data_timestamp=data_timestamp, data_age_seconds=data_age,
            provider_status=provider_status,
            provider=self._data.name if provider_status not in ("NOT_FETCHED",) else None,
            history_length=history_length,
            filter_results=tuple(evaluated_filters), preliminary_score=0.0,
            score_components=(),
            rejection_reasons=all_reasons,
            evaluation_failed=provider_status != "OK" and provider_status != "NOT_FETCHED",
        )

    def _rank(
        self,
        candidates: list[ScannerCandidate],
        accepted: list[ScannerCandidate],
    ) -> list[ScannerCandidate]:
        by_currency: dict[str, list[ScannerCandidate]] = {}
        for candidate in accepted:
            if candidate.turnover is not None:
                by_currency.setdefault(candidate.currency, []).append(candidate)
        liquidity: dict[str, float] = {}
        for group in by_currency.values():
            ordered = sorted(group, key=lambda item: (item.turnover or 0.0, item.instrument_id))
            denominator = max(1, len(ordered) - 1)
            liquidity.update({
                item.instrument_id: index / denominator
                for index, item in enumerate(ordered)
            })
        weights = (
            self._settings.liquidity_weight, self._settings.momentum_weight,
            self._settings.volume_expansion_weight, self._settings.volatility_weight,
            self._settings.quality_weight,
        )
        weight_sum = sum(weights)
        ranked = []
        for candidate in candidates:
            components = (
                ("liquidity", liquidity.get(candidate.instrument_id, 0.0)),
                ("momentum", min(1.0, max(
                    0.0, 0.5 + (candidate.momentum or 0.0) * 2.5))),
                ("volume_expansion", min(
                    1.0, max(0.0, (candidate.volume_expansion or 0.0) / 3.0))),
                ("volatility_suitability", max(0.0, 1.0 - abs(
                    (candidate.volatility or 0.0) - self._settings.volatility_target
                ) / self._settings.volatility_target)),
                ("data_quality", 1.0 if candidate.data_quality is CandidateQuality.VALID else 0.0),
            )
            score = sum(w * component for w, (_, component) in zip(weights, components)) / weight_sum
            ranked.append(replace(
                candidate,
                preliminary_score=round(score, 6),
                score_components=tuple(
                    (name, round(value, 6)) for name, value in components),
            ))
        return ranked
