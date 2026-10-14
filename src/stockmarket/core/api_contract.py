"""Shared dashboard/API compatibility contract, not trading authorization."""

from typing import Any, Mapping


DASHBOARD_CONTRACT_VERSION = 1
DASHBOARD_FEATURES = ("market_watch", "paper_market_readiness", "execution_data_status")


def dashboard_compatibility_problem(health: Mapping[str, Any] | None) -> str | None:
    if health is None:
        return "API health is unavailable; dashboard compatibility cannot be verified."
    contract = health.get("dashboard_contract")
    if not isinstance(contract, dict) or type(contract.get("version")) is not int \
            or contract["version"] != DASHBOARD_CONTRACT_VERSION:
        return "Dashboard/API version mismatch: required dashboard contract is missing or incompatible."
    features = contract.get("features")
    if not isinstance(features, list) or any(not isinstance(f, str) for f in features) \
            or not set(DASHBOARD_FEATURES).issubset(features):
        return "Dashboard/API capability mismatch: required Market Watch or readiness features are missing."
    return None
