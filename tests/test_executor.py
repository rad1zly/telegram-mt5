import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from src.parser.schema import Signal  # noqa: E402
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

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=0.01,
        max_lot_cap=5.0,
    )

    assert not result.success
    assert "Lot ditolak" in result.detail


def test_execute_signal_uses_range_midpoint_when_no_single_entry(monkeypatch):
    signal = Signal(message_id=5, action="SELL", symbol="GOLD", entry=None, entry_range=(4344.0, 4346.0), sl=4348.0, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    captured = {}

    def fake_send_order(**kwargs):
        captured.update(kwargs)
        return OrderResult(success=True, ticket=1, price=kwargs["entry"], kind="MARKET")

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
    monkeypatch.setattr(mt5_client, "send_order", fake_send_order)

    result = executor.execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=["XAUUSD"],
        risk_usd=50.0,
        max_lot_cap=5.0,
    )

    assert result.success
    assert captured["entry"] == 4345.0  # midpoint dari (4344, 4346)


def test_execute_signal_reports_order_send_failure(monkeypatch):
    signal = Signal(message_id=6, action="SELL", symbol="GOLD", entry=4344.5, sl=4348.0, tp=[4333.0])
    resolver = SymbolResolver(ALIASES)

    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda symbol: _fake_symbol_info())
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
