#!/usr/bin/env python3
"""Reconcile portfolio math from SQLite paper state (no Streamlit).

Usage:
  python debug_portfolio.py
  python debug_portfolio.py NSE
  python debug_portfolio.py US
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from stockmarket.persistence.paper_repo import get_paper_repo


def _reconcile(market: str) -> None:
    loaded = get_paper_repo(market=market).load()
    if loaded is None:
        raise SystemExit(f"No SQLite paper state found for market: {market}")
    state, _ = loaded
    cash = float(state.cash)
    start = float(state.start_capital)
    realized = float(state.realized)
    charges = float(state.charges)
    prices = {str(k): float(v) for k, v in state.prices.items()}
    holdings = state.holdings
    shorts = state.shorts

    long_mv = 0.0
    u_long = 0.0
    invested_long = 0.0
    for sym, h in holdings.items():
        qty = int(h.qty)
        avg = float(h.avg)
        ltp = float(prices.get(str(sym), avg))
        invested_long += avg * qty
        long_mv += ltp * qty
        u_long += (ltp - avg) * qty

    u_short = 0.0
    invested_short = 0.0
    for sym, h in shorts.items():
        qty = int(h.qty)
        avg = float(h.avg)
        ltp = float(prices.get(str(sym), avg))
        invested_short += avg * qty
        u_short += (avg - ltp) * qty

    unreal = u_long + u_short
    positions_mtm = long_mv + u_short
    equity_ok = cash + positions_mtm
    net_realized = realized - charges

    # Previous bug: cash + (invested long + invested short) + unreal
    equity_buggy = cash + invested_long + invested_short + unreal

    print(f"Market: {market}")
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
    market = str(sys.argv[1] if len(sys.argv) > 1 else "NSE").upper()
    _reconcile(market)


if __name__ == "__main__":
    main()
