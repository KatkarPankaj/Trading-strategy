"""Simple dashboard scan row builder."""

from stockmarket.simple_signals import scan_row_from_quote


def test_scan_row_basic():
    row = scan_row_from_quote(
        "TEST.NS",
        price=100.0,
        vwap=99.0,
        pchange=0.5,
        range_pct=1.2,
        updated_ts="12:00:00",
        score_cfg={
            "score_change_weight": 6.0,
            "score_vwap_weight": 20.0,
            "score_range_weight": 2.0,
            "ready_pchange_threshold": 0.25,
            "ready_range_threshold": 0.5,
        },
    )
    assert row["symbol"] == "TEST.NS"
    assert row["buy_score"] > 0
    assert row["sell_score"] >= 0
