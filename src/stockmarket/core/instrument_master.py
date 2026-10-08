"""Explicit, bounded development master data, separate from price providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .markets import MarketRegistry
from .models import AssetClass, Instrument
from .persistence import Store

DEVELOPMENT_MASTER = Path(__file__).with_name("development_instruments.json")


def bootstrap_instruments(
    store: Store, markets: MarketRegistry, enabled: tuple[str, ...],
    env: Mapping[str, str],
) -> dict[str, Any]:
    source = (env.get("INSTRUMENT_MASTER_FILE") or "").strip()
    profile = (env.get("INSTRUMENT_BOOTSTRAP") or "none").strip().lower()
    if profile not in {"none", "development"}:
        raise ValueError("INSTRUMENT_BOOTSTRAP must be none or development")
    if profile == "development" and env.get("PERSONAL_RESEARCH", "").lower() != "true":
        raise ValueError("development master requires PERSONAL_RESEARCH=true")
    if source and profile == "development":
        raise ValueError("choose INSTRUMENT_MASTER_FILE or development bootstrap, not both")
    path = Path(source) if source else DEVELOPMENT_MASTER if profile == "development" else None
    report: dict[str, Any] = {
        "source": "configured_file" if source else profile,
        "development_only": profile == "development",
        "requested": 0, "inserted": 0, "preserved": 0,
    }
    if path is None:
        report["reason"] = "NO_MASTER_CONFIGURED: registry contains only previously imported instruments"
        return report
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload or len(payload) > 1000:
        raise ValueError("instrument master must contain 1-1000 instrument objects")
    records: list[Instrument] = []
    identities: set[str] = set()
    allowed = {
        "symbol", "market", "mic", "asset_class", "tick_size", "lot_size",
        "provider_symbol", "active", "tradable", "shortable", "name",
    }
    for row in payload:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("instrument master contains invalid or unknown metadata fields")
        required = {"symbol", "market", "mic", "asset_class", "tick_size"}
        if not required.issubset(row):
            raise ValueError("instrument master is missing required metadata fields")
        if any(not isinstance(row[name], str) or not row[name].strip()
               for name in ("symbol", "market", "mic", "asset_class")):
            raise ValueError("instrument master identifiers must be non-empty strings")
        values = dict(row)
        market = markets.get(values.pop("market"))
        asset_class = AssetClass(values.pop("asset_class"))
        instrument = market.instrument(asset_class=asset_class, **values)
        if instrument.instrument_id in identities:
            raise ValueError(f"duplicate instrument in master: {instrument.instrument_id}")
        identities.add(instrument.instrument_id)
        if market.code in enabled:
            records.append(instrument)
    if not records:
        raise ValueError("instrument master returned no instruments for enabled markets")
    report["requested"] = len(records)
    with store.db.transaction():
        for instrument in records:
            if store.instruments.get(instrument.instrument_id) is not None:
                report["preserved"] += 1
            else:
                store.instruments.save(instrument)
                report["inserted"] += 1
    return report
