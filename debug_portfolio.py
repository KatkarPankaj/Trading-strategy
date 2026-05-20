#!/usr/bin/env python3
"""Reconcile portfolio math from dashboard_simple saved state (no Streamlit).

Usage:
  python debug_portfolio.py
  python debug_portfolio.py outputs/simple_paper_state.json
  python debug_portfolio.py outputs/simple_paper_state_us.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _reconcile(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    cash = float(data.get("cash", 0.0))
    start = float(data.get("start", 0.0))
    realized = float(data.get("realized", 0.0))
    charges = float(data.get("charges", 0.0))
    prices = {str(k): float(v) for k, v in (data.get("prices") or {}).items()}
    holdings = data.get("holdings") or {}
    shorts = data.get("shorts") or {}

    long_mv = 0.0
    u_long = 0.0
    invested_long = 0.0
    for sym, h in holdings.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(prices.get(str(sym), avg))
        invested_long += avg * qty
        long_mv += ltp * qty
        u_long += (ltp - avg) * qty

    u_short = 0.0
    invested_short = 0.0
    for sym, h in shorts.items():
        qty = int(h.get("qty", 0))
        avg = float(h.get("avg", 0.0))
        ltp = float(prices.get(str(sym), avg))
        invested_short += avg * qty
        u_short += (avg - ltp) * qty

    unreal = u_long + u_short
    positions_mtm = long_mv + u_short
    equity_ok = cash + positions_mtm
    net_realized = realized - charges

    # Previous bug: cash + (invested long + invested short) + unreal
    equity_buggy = cash + invested_long + invested_short + unreal

    print(f"State file: {path.resolve()}")
    print(f"  start_capital:           {start:,.2f}")
    print(f"  cash:                    {cash:,.2f}")
    print(f"  long cost basis (inpos): {invested_long:,.2f}")
    print(f"  long market value:       {long_mv:,.2f}")
    print(f"  short sale notional:     {invested_short:,.2f} (already inside cash)")
    print(f"  open PnL total:          {unreal:,.2f}  (long {u_long:,.2f} + short {u_short:,.2f})")
    print(f"  equity (correct):        {equity_ok:,.2f}")
    print(f"  equity - start:          {equity_ok - start:,.2f}")
    if shorts:
        print(f"  equity (old bug):        {equity_buggy:,.2f}  (double-counts short notional)")
    print(f"  realized (gross):        {realized:,.2f}")
    print(f"  charges:                 {charges:,.2f}")
    print(f"  net realized:            {net_realized:,.2f}")


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path("outputs/simple_paper_state.json")
        if not path.exists():
            alt = Path("outputs/simple_paper_state_us.json")
            if alt.exists():
                path = alt
    if not path.exists():
        raise SystemExit(f"State file not found: {path}")
    _reconcile(path)


if __name__ == "__main__":
    main()
