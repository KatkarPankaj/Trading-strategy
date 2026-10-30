"""Local, integrity-checked cache for raw Kite instrument-master observations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from .kite_client import KiteConnectClient

_REQUIRED_COLUMNS = frozenset({
    "instrument_token", "exchange_token", "tradingsymbol", "name", "last_price",
    "expiry", "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange",
})
_INDIA_EXCHANGES = frozenset({"NSE", "BSE"})
_MAX_MASTER_BYTES = 64 * 1024 * 1024
_CACHE_VERSION = 1


class KiteInstrumentMasterError(RuntimeError):
    """The cached or downloaded instrument master cannot be trusted."""


@dataclass(frozen=True, slots=True)
class KiteInstrumentRecord:
    exchange: str
    trading_symbol: str
    instrument_token: int
    segment: str
    expiry: str
    lot_size: int
    tick_size: float
    instrument_type: str
    raw: Mapping[str, str]


class KiteInstrumentMasterCache:
    """Cache full provider rows without converting them into verified platform mappings."""

    def __init__(
        self,
        client: KiteConnectClient,
        directory: Path = Path("outputs/kite"),
        *,
        max_age: timedelta = timedelta(days=1),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(directory, Path):
            raise TypeError("directory must be a Path")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("instrument master cache age must be positive")
        self._client = client
        self._directory = directory
        self._csv_path = directory / "instruments.csv"
        self._metadata_path = directory / "instruments.metadata.json"
        self._max_age = max_age
        self._clock = clock

    def get(self) -> tuple[KiteInstrumentRecord, ...]:
        """Return a fresh verified cache, or fetch it once when absent or expired."""
        csv_exists = self._exists(self._csv_path)
        metadata_exists = self._exists(self._metadata_path)
        if csv_exists != metadata_exists:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_INCOMPLETE")
        if not csv_exists:
            return self.refresh()
        raw_csv, downloaded_at, count = self._read_cache()
        age = self._now() - downloaded_at
        if age < timedelta(0):
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_TIMESTAMP_IN_FUTURE")
        if age > self._max_age:
            return self.refresh()
        records = self._parse(raw_csv)
        if len(records) != count:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_ROW_COUNT_MISMATCH")
        return records

    def refresh(self) -> tuple[KiteInstrumentRecord, ...]:
        """Explicitly request a fresh master; the client charges this through the usage guard."""
        raw_csv = self._client.download_instrument_master()
        if not raw_csv or len(raw_csv) > _MAX_MASTER_BYTES:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_RESPONSE_INVALID_SIZE")
        records = self._parse(raw_csv)
        downloaded_at = self._now()
        metadata = {
            "schema_version": _CACHE_VERSION,
            "downloaded_at": downloaded_at.isoformat(),
            "sha256": hashlib.sha256(raw_csv).hexdigest(),
            "row_count": len(records),
        }
        self._write_cache(raw_csv, metadata)
        return records

    @staticmethod
    def for_exchange(
        records: tuple[KiteInstrumentRecord, ...], exchange: str,
    ) -> tuple[KiteInstrumentRecord, ...]:
        if not isinstance(exchange, str):
            raise TypeError("exchange must be a string")
        if exchange not in _INDIA_EXCHANGES:
            raise ValueError("exchange must be NSE or BSE")
        return tuple(record for record in records if record.exchange == exchange)

    @classmethod
    def find(
        cls, records: tuple[KiteInstrumentRecord, ...], exchange: str, trading_symbol: str,
    ) -> KiteInstrumentRecord:
        if not isinstance(trading_symbol, str) or not trading_symbol.strip():
            raise ValueError("trading_symbol must be non-empty")
        matches = tuple(
            record for record in cls.for_exchange(records, exchange)
            if record.trading_symbol == trading_symbol.strip()
        )
        if not matches:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_MAPPING_NOT_FOUND")
        if len(matches) != 1:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_MAPPING_AMBIGUOUS")
        return matches[0]

    def _parse(self, raw_csv: bytes) -> tuple[KiteInstrumentRecord, ...]:
        try:
            text = raw_csv.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(reader.fieldnames):
                raise KiteInstrumentMasterError("KITE_INSTRUMENT_COLUMNS_INVALID")
            records: list[KiteInstrumentRecord] = []
            seen_tokens: set[tuple[str, int]] = set()
            for row in reader:
                exchange = (row.get("exchange") or "").strip().upper()
                if exchange not in _INDIA_EXCHANGES:
                    continue
                try:
                    token = int(row["instrument_token"])
                    lot_size = int(row["lot_size"])
                    tick_size = float(row["tick_size"])
                except (KeyError, TypeError, ValueError):
                    raise KiteInstrumentMasterError(
                        "KITE_INSTRUMENT_NUMERIC_FIELD_INVALID") from None
                symbol = (row.get("tradingsymbol") or "").strip()
                segment = (row.get("segment") or "").strip()
                instrument_type = (row.get("instrument_type") or "").strip()
                if token <= 0 or lot_size <= 0 or not math.isfinite(tick_size) \
                        or tick_size <= 0 or not symbol or not segment or not instrument_type:
                    raise KiteInstrumentMasterError("KITE_INSTRUMENT_ROW_INVALID")
                identity = (exchange, token)
                if identity in seen_tokens:
                    raise KiteInstrumentMasterError("KITE_INSTRUMENT_TOKEN_DUPLICATE")
                seen_tokens.add(identity)
                values = {key: value or "" for key, value in row.items() if key is not None}
                records.append(KiteInstrumentRecord(
                    exchange=exchange,
                    trading_symbol=symbol,
                    instrument_token=token,
                    segment=segment,
                    expiry=values.get("expiry", "").strip(),
                    lot_size=lot_size,
                    tick_size=tick_size,
                    instrument_type=instrument_type,
                    raw=MappingProxyType(values),
                ))
        except UnicodeDecodeError:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_ENCODING_INVALID") from None
        except csv.Error:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CSV_INVALID") from None
        if not records:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_INDIA_ROWS_MISSING")
        return tuple(records)

    def _read_cache(self) -> tuple[bytes, datetime, int]:
        try:
            raw_csv = self._csv_path.read_bytes()
            metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise TypeError
            downloaded_at = datetime.fromisoformat(metadata["downloaded_at"])
            count = metadata["row_count"]
            digest = metadata["sha256"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_UNREADABLE") from None
        if metadata.get("schema_version") != _CACHE_VERSION \
                or not isinstance(count, int) or isinstance(count, bool) or count < 1 \
                or not isinstance(digest, str) \
                or hashlib.sha256(raw_csv).hexdigest() != digest \
                or downloaded_at.tzinfo is None or downloaded_at.utcoffset() is None:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_INTEGRITY_FAILED")
        if len(raw_csv) > _MAX_MASTER_BYTES:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_TOO_LARGE")
        return raw_csv, downloaded_at.astimezone(timezone.utc), count

    def _write_cache(self, raw_csv: bytes, metadata: Mapping[str, object]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        metadata_bytes = json.dumps(
            metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._atomic_write(self._csv_path, raw_csv)
        self._atomic_write(self._metadata_path, metadata_bytes)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError("instrument cache clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            path.stat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise KiteInstrumentMasterError("KITE_INSTRUMENT_CACHE_ACCESS_FAILED") from None
