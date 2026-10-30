"""Local, provenance-preserving CSV input for historical NSE replay."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..data.provider import INTERVALS
from ..data.quality import validate_bars
from ..models import AssetClass, Instrument
from ..trading_calendar import TradingCalendar
from .engine import BacktestEngine, BacktestResult, SignalFn

MAX_CSV_BYTES = 50 * 1024 * 1024
MAX_CSV_ROWS = 500_000
CSV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class HistoricalCSVImportError(ValueError):
    """A local CSV was rejected; `provenance` describes the recorded attempt."""

    def __init__(self, message: str, provenance: dict[str, Any] | None = None) -> None:
        self.provenance = provenance or {}
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ImportedHistoricalDataset:
    instrument: Instrument
    interval: str
    bars: pd.DataFrame
    provenance: dict[str, Any]

    def replay(
        self,
        engine: BacktestEngine,
        signal_fn: SignalFn,
        *,
        calendar: TradingCalendar,
    ) -> BacktestResult:
        """Replay accepted bars through the existing engine, never a broker."""
        if not isinstance(engine, BacktestEngine):
            raise TypeError("engine must be a BacktestEngine")
        if engine.config.base_currency.upper() != "INR":
            raise ValueError("NSE historical replay requires an INR backtest")
        if self.instrument.market != "IN" or self.instrument.exchange != "XNSE" \
                or self.instrument.currency != "INR":
            raise ValueError("historical replay dataset is not NSE/IN/INR")
        if calendar.timezone != self.instrument.timezone:
            raise ValueError("NSE replay calendar timezone does not match the instrument")
        if self.provenance.get("validation_status") != "VALIDATED" \
                or self.provenance.get("source_provenance") != "USER_DECLARED_UNVERIFIED" \
                or self.provenance.get("instrument_id") != self.instrument.instrument_id \
                or self.provenance.get("exchange") != "XNSE" \
                or self.provenance.get("currency") != "INR" \
                or self.provenance.get("interval") != self.interval \
                or self.provenance.get("accepted_bar_count") != len(self.bars) \
                or self.provenance.get("rejected_bar_count") != 0 \
                or self.provenance.get("accepted_bars_sha256") != _bars_sha256(self.bars):
            raise ValueError("only validated historical CSV imports can be replayed")

        local_days = set(self.bars.index.tz_convert(self.instrument.timezone).date)
        uncovered = sorted(day for day in local_days if not calendar.is_covered(day))
        if uncovered:
            raise ValueError(
                "NSE replay calendar does not cover imported dates; "
                "calendar review is separate from historical CSV validation")
        closed_bars = [stamp for stamp in self.bars.index if not calendar.is_open(stamp)]
        if closed_bars:
            raise ValueError(
                f"CSV contains {len(closed_bars)} bars outside the covered NSE calendar")

        first_day, last_day = min(local_days), max(local_days)
        return engine.run(
            {self.instrument.instrument_id: self.instrument},
            {self.instrument.instrument_id: self.bars},
            signal_fn,
            calendars={self.instrument.instrument_id: calendar},
            start=first_day,
            end=last_day,
        )


def import_nse_csv(
    csv_path: str | Path,
    instrument: Instrument,
    interval: str,
    *,
    declared_source: str,
    artifact_dir: str | Path = Path("outputs") / "historical_replay",
    imported_at: datetime | None = None,
) -> ImportedHistoricalDataset:
    """Validate a strict ISO-8601 OHLCV CSV and preserve it with a JSON manifest.

    Required header: timestamp,open,high,low,close,volume. Timestamps must include
    a UTC offset or `Z`; accepted bars are normalized to UTC.
    """
    if not isinstance(instrument, Instrument):
        raise TypeError("instrument must be a registered Instrument")
    if (instrument.market, instrument.exchange, instrument.currency,
            instrument.asset_class) != ("IN", "XNSE", "INR", AssetClass.EQUITY):
        raise ValueError("CSV import supports NSE cash equities only (XNSE, INR)")
    if instrument.instrument_id != f"XNSE:{instrument.symbol}":
        raise ValueError("instrument ID must use the canonical XNSE:<symbol> form")
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; use one of {sorted(INTERVALS)}")
    if not isinstance(declared_source, str) or not declared_source.strip():
        raise ValueError("declared_source must identify the user-reported data source")
    timestamp = imported_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")

    source_path = Path(csv_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"historical CSV does not exist: {source_path}")
    size = source_path.stat().st_size
    if size <= 0 or size > MAX_CSV_BYTES:
        raise ValueError(f"CSV file size must be between 1 byte and {MAX_CSV_BYTES} bytes")
    digest = _sha256_file(source_path)
    artifact_id = uuid.uuid4().hex
    destination = Path(artifact_dir) / artifact_id
    destination.mkdir(parents=True, exist_ok=False)
    preserved_file = destination / source_path.name
    shutil.copy2(source_path, preserved_file)
    if _sha256_file(preserved_file) != digest:
        raise OSError("source CSV changed while it was being preserved; import aborted")

    report: dict[str, Any] = {
        "artifact_id": artifact_id,
        "original_filename": source_path.name,
        "original_sha256": digest,
        "preserved_file": preserved_file.name,
        "imported_at": timestamp.astimezone(timezone.utc).isoformat(),
        "instrument_id": instrument.instrument_id,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "market": instrument.market,
        "currency": instrument.currency,
        "interval": interval,
        "declared_source": declared_source.strip(),
        "source_provenance": "USER_DECLARED_UNVERIFIED",
        "imported_bar_count": 0,
        "accepted_bar_count": 0,
        "rejected_bar_count": 0,
        "date_range_utc": None,
        "internal_gaps": "CHECKED",
        "session_boundary_completeness": "NOT_VERIFIED",
        "validation_status": "REJECTED",
    }

    try:
        frame = pd.read_csv(
            source_path, encoding="utf-8-sig", dtype=str, keep_default_na=False,
            on_bad_lines="error")
        report["imported_bar_count"] = int(len(frame))
        report["rejected_bar_count"] = int(len(frame))
        normalized = [str(column).strip().lower() for column in frame.columns]
        if len(normalized) != len(set(normalized)):
            raise ValueError("CSV has duplicate column names after case normalization")
        if tuple(normalized) != CSV_COLUMNS:
            raise ValueError(
                "CSV header must be exactly timestamp,open,high,low,close,volume "
                "(column names are case-insensitive and order matters)")
        if frame.empty:
            raise ValueError("CSV contains no OHLCV rows")
        if len(frame) > MAX_CSV_ROWS:
            raise ValueError(f"CSV exceeds the {MAX_CSV_ROWS}-bar import limit")

        frame.columns = list(CSV_COLUMNS)
        parsed_timestamps = []
        for row_number, raw in enumerate(frame["timestamp"], start=2):
            try:
                parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(
                    f"row {row_number}: timestamp must be ISO-8601 with an offset or Z") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(
                    f"row {row_number}: timestamp has no timezone; include an offset or Z")
            parsed_timestamps.append(parsed.astimezone(timezone.utc))

        bars = frame.loc[:, ["open", "high", "low", "close", "volume"]].copy()
        for column in bars.columns:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars.index = pd.DatetimeIndex(parsed_timestamps, name="timestamp")
        quality = validate_bars(bars, interval, timezone=instrument.timezone)
        if not quality.ok:
            raise ValueError("historical bars rejected: " + ", ".join(quality.issues))
        _validate_session_alignment(bars, instrument, interval)

        report.update({
            "accepted_bar_count": int(len(bars)),
            "rejected_bar_count": 0,
            "accepted_bars_sha256": _bars_sha256(bars),
            "date_range_utc": {
                "start": bars.index.min().isoformat(),
                "end": bars.index.max().isoformat(),
            },
            "validation_status": "VALIDATED",
        })
        _write_manifest(destination, report)
        return ImportedHistoricalDataset(instrument, interval, bars, report)
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError, TypeError) as exc:
        if report["imported_bar_count"]:
            report["rejected_bar_count"] = report["imported_bar_count"]
        report["rejection_reason"] = str(exc)
        _write_manifest(destination, report)
        raise HistoricalCSVImportError(str(exc), report) from exc


def _validate_session_alignment(
    bars: pd.DataFrame, instrument: Instrument, interval: str,
) -> None:
    if interval == "1d":
        return
    if instrument.trading_hours is None:
        raise ValueError("instrument must define local NSE trading hours")
    step = INTERVALS[interval]
    open_time, close_time = instrument.trading_hours
    local_index = bars.index.tz_convert(instrument.timezone)
    invalid = []
    for stamp in local_index:
        local_time = stamp.time().replace(tzinfo=None)
        elapsed = (
            local_time.hour * 3600 + local_time.minute * 60 + local_time.second
            - (open_time.hour * 3600 + open_time.minute * 60 + open_time.second)
        )
        if local_time < open_time or local_time >= close_time \
                or elapsed < 0 or elapsed % int(step.total_seconds()) != 0:
            invalid.append(stamp)
    if invalid:
        raise ValueError(
            f"{len(invalid)} bars fall outside NSE hours or the {interval} bar grid")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bars_sha256(bars: pd.DataFrame) -> str:
    return hashlib.sha256(bars.to_csv().encode("utf-8")).hexdigest()


def _write_manifest(directory: Path, report: dict[str, Any]) -> None:
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(
        json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    temporary.replace(directory / "manifest.json")
