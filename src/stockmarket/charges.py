"""Shared NSE intraday charge model (paper / simulator)."""


def intraday_charges_nse(side: str, turnover: float) -> float:
    brokerage = min(turnover * 0.0003, 20.0)
    exchange_txn = turnover * 0.0000325
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange_txn + sebi)
    stamp = turnover * 0.00003 if side == "BUY" else 0.0
    stt = turnover * 0.00025 if side == "SELL" else 0.0
    return brokerage + exchange_txn + sebi + gst + stamp + stt
