import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from src.trading import mt5_client  # noqa: E402
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


def test_sell_is_market_when_price_already_below_level():
    # "Sell while below 4350" + harga sudah 4344 (di BAWAH 4350) = syarat
    # SUDAH terpenuhi -> masuk SEKARANG. Dulu ini jadi SELL_LIMIT, yaitu
    # menunggu harga NAIK balik ke 4350 -- kebalikan dari maksud channel.
    assert decide_order_kind("SELL", entry=4350.0, current_price=4344.0, tolerance=0.1) == "MARKET"


def test_buy_stop_when_entry_above_current_price():
    assert decide_order_kind("BUY", entry=4350.0, current_price=4344.0, tolerance=0.1) == "BUY_STOP"


def test_buy_is_market_when_price_already_above_level():
    # "Buy while above 4344" + harga sudah 4350 (di ATAS 4344) = syarat
    # SUDAH terpenuhi -> masuk SEKARANG, bukan menunggu harga turun balik.
    assert decide_order_kind("BUY", entry=4344.0, current_price=4350.0, tolerance=0.1) == "MARKET"


def test_limit_orders_are_never_produced():
    """Channel ini tidak pernah bermaksud limit: level yang disebut adalah
    SYARAT di sekitar harga sekarang, bukan target yang ditunggu dari arah
    berlawanan. Kunci invarian ini supaya tidak diam-diam balik lagi."""
    for direction in ("BUY", "SELL"):
        for current in (4300.0, 4344.0, 4400.0):
            kind = decide_order_kind(direction, entry=4344.0, current_price=current, tolerance=0.1)
            assert kind in ("MARKET", "BUY_STOP", "SELL_STOP"), kind


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


# --- partial_close: sumber data broker bisa None ---
# send_order sudah menjaga symbol_info_tick/symbol_info yang None sejak
# awal, tapi partial_close dulu tidak -- feed putus di jalur MENUTUP
# posisi (termasuk close_all) berujung AttributeError yang membatalkan
# seluruh penanganan follow-up. Justru jalur ini yang paling tidak boleh
# gagal diam-diam.

class _FakePosition:
    type = 1  # ORDER_TYPE_SELL


class _FakeMT5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    ORDER_FILLING_IOC = 2

    def __init__(self, tick=None, info=None):
        self._tick = tick
        self._info = info
        self.orders_sent = []

    def positions_get(self, ticket=None):
        return [_FakePosition()]

    def symbol_info_tick(self, symbol):
        return self._tick

    def symbol_info(self, symbol):
        return self._info

    def order_send(self, request):
        self.orders_sent.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=1, price=100.0)

    def last_error(self):
        return "fake error"


def test_partial_close_fails_gracefully_when_tick_unavailable(monkeypatch):
    fake = _FakeMT5(tick=None, info=SimpleNamespace(filling_mode=2, visible=True))
    monkeypatch.setattr(mt5_client, "_mt5", lambda: fake)

    result = mt5_client.partial_close(ticket=1, symbol="XAUUSD+", volume=0.1)

    assert result.success is False
    assert "tick" in result.error.lower()
    assert fake.orders_sent == []  # tidak boleh terlanjur kirim order


def test_partial_close_fails_gracefully_when_symbol_info_unavailable(monkeypatch):
    fake = _FakeMT5(tick=SimpleNamespace(ask=100.5, bid=100.0), info=None)
    monkeypatch.setattr(mt5_client, "_mt5", lambda: fake)

    result = mt5_client.partial_close(ticket=1, symbol="XAUUSD+", volume=0.1)

    assert result.success is False
    assert "symbol_info" in result.error
    assert fake.orders_sent == []


def test_partial_close_succeeds_with_valid_broker_data(monkeypatch):
    fake = _FakeMT5(
        tick=SimpleNamespace(ask=100.5, bid=100.0),
        info=SimpleNamespace(filling_mode=2, visible=True),
    )
    monkeypatch.setattr(mt5_client, "_mt5", lambda: fake)

    result = mt5_client.partial_close(ticket=1, symbol="XAUUSD+", volume=0.1)

    assert result.success is True
    assert len(fake.orders_sent) == 1
    # posisi SELL ditutup dengan BUY di harga ASK
    assert fake.orders_sent[0]["type"] == fake.ORDER_TYPE_BUY
    assert fake.orders_sent[0]["price"] == 100.5
