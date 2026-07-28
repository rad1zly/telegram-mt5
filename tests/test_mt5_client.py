import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from src.trading.mt5_client import (  # noqa: E402
    compute_market_tolerance,
    decide_order_kind,
    pip_size,
)


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


def test_pip_size_for_5_digit_broker():
    info = SimpleNamespace(point=0.00001, digits=5)
    assert pip_size(info) == 0.0001


def test_pip_size_for_2_digit_broker():
    info = SimpleNamespace(point=0.01, digits=2)
    assert pip_size(info) == 0.01


def test_compute_market_tolerance_uses_business_config_when_larger():
    # XAUUSD 2 digit, point=0.01, stops_level kecil (misal 0)
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=0)
    tolerance = compute_market_tolerance(info, max_deviation_pips=15.0)
    # business_tolerance = 15 * 0.01 = 0.15; broker_min = (0+5)*0.01 = 0.05 -> pakai yang lebih besar
    assert tolerance == 0.15


def test_compute_market_tolerance_uses_broker_stops_level_when_larger():
    # broker minta jarak minimum 200 poin -> jauh lebih besar dari toleransi config
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=200)
    tolerance = compute_market_tolerance(info, max_deviation_pips=15.0)
    assert tolerance == (200 + 5) * 0.01


def test_scenario_call_1_point_below_with_default_pips_still_pending():
    # Gold 2-digit: pip_size == point == 0.01, jadi 15 "pips" cuma $0.15 —
    # TIDAK cukup untuk menutup gap $1 di skenario nyata (entry 4020, harga
    # sekarang 4019). Ini limitasi yang didokumentasikan: satu angka
    # max_price_deviation_pips dipakai bareng untuk FX & metals/index yang
    # skala pip-nya beda jauh. Test ini sengaja menunjukkan itu, BUKAN
    # menganggap ini perilaku ideal — lihat catatan di compute_market_tolerance.
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=0)
    tolerance = compute_market_tolerance(info, max_deviation_pips=15.0)
    kind = decide_order_kind("BUY", entry=4020.0, current_price=4019.0, tolerance=tolerance)
    assert kind == "BUY_STOP"


def test_scenario_call_1_point_below_becomes_market_with_wider_config():
    # kalau max_price_deviation_pips dinaikkan (mis. 100) khusus untuk
    # instrumen seperti gold, gap $1 baru dianggap 'sudah di harga'.
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=0)
    tolerance = compute_market_tolerance(info, max_deviation_pips=100.0)
    kind = decide_order_kind("BUY", entry=4020.0, current_price=4019.0, tolerance=tolerance)
    assert kind == "MARKET"
