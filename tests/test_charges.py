from stockmarket.charges import intraday_charges_nse


def test_intraday_charges_positive():
    c = intraday_charges_nse("BUY", 100_000.0)
    assert c > 0
    assert c < 5000.0
