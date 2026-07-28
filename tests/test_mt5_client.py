import sys

sys.path.insert(0, ".")

from src.trading.mt5_client import decide_order_kind  # noqa: E402


def test_market_when_within_tolerance():
    assert decide_order_kind("SELL", entry=4344.8, current_price=4344.8, tolerance=0.5) == "MARKET"
    assert decide_order_kind("BUY", entry=4345.0, current_price=4344.8, tolerance=0.5) == "MARKET"


def test_sell_stop_when_entry_below_current_price():
    # breakout ke bawah: harga sekarang 4350, entry 4344 -> tunggu breakout turun
    assert decide_order_kind("SELL", entry=4344.0, current_price=4350.0, tolerance=0.1) == "SELL_STOP"


def test_sell_limit_when_entry_above_current_price():
    # pullback ke atas dulu baru sell: harga sekarang 4344, entry 4350
    assert decide_order_kind("SELL", entry=4350.0, current_price=4344.0, tolerance=0.1) == "SELL_LIMIT"


def test_buy_stop_when_entry_above_current_price():
    assert decide_order_kind("BUY", entry=4350.0, current_price=4344.0, tolerance=0.1) == "BUY_STOP"


def test_buy_limit_when_entry_below_current_price():
    assert decide_order_kind("BUY", entry=4344.0, current_price=4350.0, tolerance=0.1) == "BUY_LIMIT"
