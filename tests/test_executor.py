import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from src.parser.schema import Signal, apply_price_offset  # noqa: E402
from src.trading import executor, mt5_client  # noqa: E402
from src.trading.mt5_client import OrderResult  # noqa: E402
from src.trading.symbols import SymbolResolver  # noqa: E402

ALIASES = {"XAUUSD": ["GOLD", "XAUUSD"]}


def _fake_symbol_info(**overrides):
    defaults = dict(
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_step=0.01,
        volume_min=0.01,
        volume_max=100.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_execute_signal_happy_path(monkeypatch):
    signal = Signal(message_id=1, action="SELL", symbol="GOLD", entry=4344.5, sl=4348.0, tp=[4333.0, 4323.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4344.5)
    monkeypatch.setattr(
        mt5_client,
        "send_order",
        lambda **kwargs: OrderResult(success=True, ticket=12345, price=4344.5, kind="MARKET"),
    )

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert result.success
    assert result.ticket == 12345
    assert "XAUUSD" in result.detail


def test_execute_signal_rejected_when_symbol_unresolvable():
    signal = Signal(message_id=2, action="SELL", symbol="UNKNOWNSYM", entry=100.0, sl=110.0, tp=[90.0])
    resolver = SymbolResolver(ALIASES)

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "Simbol ditolak" in result.detail


def test_execute_signal_rejected_when_no_sl():
    signal = Signal(message_id=3, action="SELL", symbol="GOLD", entry=4344.5, sl=None, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "SL" in result.detail


def test_execute_signal_rejected_when_lot_below_volume_min(monkeypatch):
    # risk kecil banget relatif ke jarak SL -> lot hasil hitungan di bawah volume_min
    signal = Signal(message_id=4, action="SELL", symbol="GOLD", entry=100.0, sl=200.0, tp=[50.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 100.0)

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=0.01,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "Lot ditolak" in result.detail


def test_execute_signal_reports_order_send_failure(monkeypatch):
    signal = Signal(message_id=6, action="SELL", symbol="GOLD", entry=4344.5, sl=4348.0, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4344.5)
    monkeypatch.setattr(
        mt5_client,
        "send_order",
        lambda **kwargs: OrderResult(success=False, error="retcode=10004 (Requote)"),
    )

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "Order gagal" in result.detail


def test_execute_signal_rejected_when_price_unavailable(monkeypatch):
    signal = Signal(message_id=7, action="SELL", symbol="GOLD", entry=4344.5, sl=4348.0, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: None)

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "harga live" in result.detail


class TestEntryRangeResolution:
    """4020-4025: harga di dalam rentang -> market di harga sekarang;
    di luar rentang -> sisi TERDEKAT dari rentang, bukan titik tengah."""

    def _run(self, monkeypatch, current_price):
        signal = Signal(message_id=8, action="BUY", symbol="GOLD", entry=None, entry_range=(4020.0, 4025.0), sl=4010.0, tp=[4040.0])
        resolver = SymbolResolver(ALIASES)

        captured = {}

        def fake_send_order(**kwargs):
            captured.update(kwargs)
            return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

        monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
        monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: current_price)
        monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

        result = executor.execute_signal(
            signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
            risk_usd=50.0, max_lot_cap=5.0,
        )
        assert result.success
        return captured["entry"]

    def test_price_inside_range_uses_current_price(self, monkeypatch):
        assert self._run(monkeypatch, current_price=4022.0) == 4022.0

    def test_price_below_range_uses_nearest_low_edge(self, monkeypatch):
        assert self._run(monkeypatch, current_price=4015.0) == 4020.0

    def test_price_above_range_uses_nearest_high_edge(self, monkeypatch):
        assert self._run(monkeypatch, current_price=4030.0) == 4025.0


def test_price_deviation_override_reaches_send_order(monkeypatch):
    # canonical XAUUSD punya override 100 pips -> harus dipakai, bukan
    # max_price_deviation_pips global (15)
    signal = Signal(message_id=9, action="BUY", symbol="GOLD", entry=4020.0, sl=4010.0, tp=[4040.0])
    resolver = SymbolResolver(ALIASES)

    captured = {}

    def fake_send_order(**kwargs):
        captured.update(kwargs)
        return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4019.0)
    monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

    executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
        risk_usd=50.0, max_lot_cap=5.0,
        max_price_deviation_pips=15.0,
        price_deviation_overrides={"XAUUSD": 100.0},
    )

    assert captured["max_deviation_pips"] == 100.0


def test_price_deviation_override_absent_falls_back_to_global(monkeypatch):
    signal = Signal(message_id=10, action="BUY", symbol="GOLD", entry=4020.0, sl=4010.0, tp=[4040.0])
    resolver = SymbolResolver(ALIASES)

    captured = {}

    def fake_send_order(**kwargs):
        captured.update(kwargs)
        return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4019.0)
    monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

    executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
        risk_usd=50.0, max_lot_cap=5.0,
        max_price_deviation_pips=15.0,
        price_deviation_overrides={"EURUSD": 50.0},  # simbol lain, tidak match
    )

    assert captured["max_deviation_pips"] == 15.0


def test_min_sl_distance_override_rejects_too_tight_sl(monkeypatch):
    # entry-sl cuma jarak 1.0, override XAUUSD minta minimum 2.0 -> ditolak
    signal = Signal(message_id=11, action="BUY", symbol="GOLD", entry=4342.0, sl=4341.0, tp=[4360.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4342.0)

    result = executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
        risk_usd=50.0, max_lot_cap=5.0,
        min_sl_distance_overrides={"XAUUSD": 2.0},
    )

    assert not result.success
    assert "Lot ditolak" in result.detail


def test_apply_price_offset_shifts_entry_sl_tp_in_parallel():
    signal = Signal(message_id=1, action="SELL", symbol="US30", entry=51500.0, sl=51550.0, tp=[51400.0, 51300.0])
    shifted = apply_price_offset(signal, 10.0)

    assert shifted.entry == 51510.0
    assert shifted.sl == 51560.0
    assert shifted.tp == [51410.0, 51310.0]
    # jarak relatif entry-ke-sl dan entry-ke-tp TIDAK berubah
    assert shifted.sl - shifted.entry == signal.sl - signal.entry
    assert shifted.tp[0] - shifted.entry == signal.tp[0] - signal.entry


def test_apply_price_offset_shifts_entry_range_too():
    signal = Signal(message_id=2, action="BUY", symbol="NAS100", entry=None, entry_range=(24000.0, 24010.0), sl=23950.0, tp=[24100.0])
    shifted = apply_price_offset(signal, 7.0)

    assert shifted.entry_range == (24007.0, 24017.0)


def test_apply_price_offset_zero_returns_same_signal_unchanged():
    signal = Signal(message_id=3, action="SELL", symbol="XAUUSD", entry=4344.0, sl=4348.0, tp=[4333.0])
    shifted = apply_price_offset(signal, 0.0)

    assert shifted.entry == signal.entry
    assert shifted.sl == signal.sl
    assert shifted.tp == signal.tp


def test_execute_signal_applies_price_offset_before_lot_and_order(monkeypatch):
    # US30 dgn offset +10 -- broker kita konsisten $10 lebih tinggi dari
    # referensi channel (ditemukan lewat perbandingan manual live). Entry,
    # SL, TP harus digeser SEBELUM dipakai hitung lot & kirim order.
    signal = Signal(message_id=13, action="SELL", symbol="US30", entry=51500.0, sl=51550.0, tp=[51400.0])
    resolver = SymbolResolver({"US30": ["US30"]})

    captured = {}

    def fake_send_order(**kwargs):
        captured.update(kwargs)
        return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 51510.0)
    monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

    result = executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["US30"],
        risk_usd=50.0, max_lot_cap=5.0,
        price_offset_overrides={"US30": 10.0},
    )

    assert result.success
    assert captured["entry"] == 51510.0  # entry via harga live (sudah broker-native)
    assert captured["sl"] == 51560.0  # SL channel + offset
    assert captured["tp"] == 51410.0  # TP channel + offset


def test_execute_signal_no_offset_when_symbol_not_in_overrides(monkeypatch):
    signal = Signal(message_id=14, action="SELL", symbol="GOLD", entry=4344.0, sl=4348.0, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    captured = {}

    def fake_send_order(**kwargs):
        captured.update(kwargs)
        return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4344.0)
    monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

    executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
        risk_usd=50.0, max_lot_cap=5.0,
        price_offset_overrides={"US30": 10.0},  # simbol lain, tidak match
    )

    assert captured["sl"] == 4348.0  # tidak digeser sama sekali


def test_min_sl_distance_override_absent_no_guard(monkeypatch):
    # simbol tidak ada di override -> default 0.0, tidak ada guard sama sekali
    signal = Signal(message_id=12, action="BUY", symbol="GOLD", entry=4342.0, sl=4341.9, tp=[4360.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "get_current_price", lambda symbol, direction: 4342.0)
    monkeypatch.setattr(
        mt5_client, "send_order",
        lambda **kwargs: OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET"),
    )

    result = executor.execute_signal(
        signal=signal, resolver=resolver, broker_symbols=["XAUUSD"],
        risk_usd=50.0, max_lot_cap=5.0,
        min_sl_distance_overrides={"EURUSD": 0.0015},  # simbol lain
    )

    assert result.success
