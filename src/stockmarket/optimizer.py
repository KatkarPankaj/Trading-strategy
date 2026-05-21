"""Backward-compatible shim — import from stockmarket.optimization."""

from __future__ import annotations

import warnings

from stockmarket.optimization import *  # noqa: F403, F401
from stockmarket.optimization import (
    benchmark_symbol_for_cfg as _benchmark_symbol_for_cfg,
)

warnings.warn(
    "Import from stockmarket.optimization instead of stockmarket.optimizer",
    DeprecationWarning,
    stacklevel=2,
)
