"""Research-only NSE sector-index observations from NSE's public indices page API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from math import isfinite
from typing import Callable, Literal, Mapping
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from ..models import Instrument
from ..research import ResearchObservation

NSE_HOME = "https://www.nseindia.com/"
NSE_INDEX_API = "https://www.nseindia.com/api/allIndices"
NSE_INDEX_PAGE = "https://www.nseindia.com/market-data/live-market-indices"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class NSESectorDataUnavailable(RuntimeError):
    """NSE data could not be retrieved or did not satisfy the expected contract."""


IndexSnapshotLoader = Callable[[], Mapping[str, object]]


def _load_official_snapshot() -> Mapping[str, object]:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    landing = Request(NSE_HOME, headers={"User-Agent": _USER_AGENT})
    api = Request(
        NSE_INDEX_API,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_INDEX_PAGE,
        },
    )
    try:
        with opener.open(landing, timeout=10) as response:
            response.read(256 * 1024)
        with opener.open(api, timeout=10) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise NSESectorDataUnavailable("NSE index response exceeded the size limit")
        payload = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raise NSESectorDataUnavailable(
            f"NSE index endpoint returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NSESectorDataUnavailable(
            f"NSE index request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise NSESectorDataUnavailable("NSE index endpoint returned an invalid payload")
    return payload


class NSESectorIndexObservationProvider:
    """Resolve an explicitly configured NSE symbol→index map against NSE live indices."""

    name = "nse_sector_indices"

    def __init__(
        self,
        symbol_to_index: Mapping[str, str],
        *,
        snapshot_loader: IndexSnapshotLoader = _load_official_snapshot,
    ) -> None:
        if not isinstance(symbol_to_index, Mapping) or not symbol_to_index:
            raise ValueError("symbol_to_index must be a non-empty mapping")
        normalized: dict[str, str] = {}
        for symbol, index_name in symbol_to_index.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("sector map symbols must be non-empty strings")
            if not isinstance(index_name, str) or not index_name.strip():
                raise ValueError("sector map index names must be non-empty strings")
            key = symbol.strip().upper()
            if key in normalized:
                raise ValueError(f"duplicate sector map symbol: {key}")
            normalized[key] = " ".join(index_name.split()).upper()
        self._symbol_to_index = normalized
        self._snapshot_loader = snapshot_loader

    def get_observation(
        self,
        instrument: Instrument,
        *,
        component: Literal["sector", "fundamental"],
        as_of: datetime,
        max_age: timedelta,
    ) -> ResearchObservation | None:
        if component != "sector":
            return None
        if not isinstance(as_of, datetime) or as_of.tzinfo is None \
                or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta")

        index_name = self._symbol_to_index.get(instrument.symbol.strip().upper())
        if index_name is None:
            return None
        if instrument.market != "IN" or instrument.exchange != "XNSE":
            raise NSESectorDataUnavailable(
                "NSE sector mappings apply only to NSE (XNSE) instruments")

        try:
            snapshot = self._snapshot_loader()
        except NSESectorDataUnavailable:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise NSESectorDataUnavailable(
                f"NSE index request failed: {type(exc).__name__}") from exc
        if not isinstance(snapshot, Mapping):
            raise NSESectorDataUnavailable("NSE index snapshot is not an object")
        rows = snapshot.get("data")
        raw_timestamp = snapshot.get("timestamp")
        if not isinstance(rows, list) or not isinstance(raw_timestamp, str):
            raise NSESectorDataUnavailable("NSE index snapshot has an invalid shape")
        try:
            observed_at = datetime.strptime(
                raw_timestamp, "%d-%b-%Y %H:%M").replace(
                    tzinfo=ZoneInfo("Asia/Kolkata"))
        except ValueError as exc:
            raise NSESectorDataUnavailable(
                "NSE index snapshot has an invalid timestamp") from exc

        matching = [
            row for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("index"), str)
            and " ".join(row["index"].split()).upper() == index_name
        ]
        if len(matching) != 1:
            raise NSESectorDataUnavailable(
                f"NSE index snapshot did not contain exactly one {index_name!r} row")
        row = matching[0]
        value = row.get("last")
        change = row.get("percentChange")
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not isfinite(value) or value <= 0:
            raise NSESectorDataUnavailable("NSE index row has an invalid last value")
        if isinstance(change, bool) or not isinstance(change, (int, float)) \
                or not isfinite(change):
            raise NSESectorDataUnavailable("NSE index row has an invalid percent change")

        return ResearchObservation(
            instrument_id=instrument.instrument_id,
            market=instrument.market,
            component="sector",
            subject=f"NSE sector index: {index_name}",
            content=(
                f"Official NSE index {index_name}; last value {value}; "
                f"reported daily percent change {change}%; "
                f"snapshot timestamp {observed_at.isoformat()}."
            ),
            observed_at=observed_at,
            source=self.name,
            reference=(
                "https://www.nseindia.com/market-data/live-equity-market"
                f"?symbol={quote(index_name, safe='')}"
            ),
        )
