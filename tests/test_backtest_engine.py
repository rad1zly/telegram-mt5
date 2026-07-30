import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from backtest.engine import (  # noqa: E402
    SimulatedTrade,
    SymbolSpec,
    pnl_usd,
    resolve_entry_fill,
    resolve_trade_up_to,
)
from backtest.price_data import Candle, PriceSeries  # noqa: E402

T0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)


def _series(rows):
    """rows: list of (menit_ke_berapa_dari_T0, open, high, low, close)"""
    candles = [
        Candle(time=T0 + timedelta(minutes=m), open=o, high=h, low=lo, close=c)
        for m, o, h, lo, c in rows
    ]
    return PriceSeries(candles)


def _spec(**overrides):
    defaults = dict(
        broker_symbol="XAUUSD+", point=0.01, digits=2,
        trade_tick_size=0.01, trade_tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        trade_stops_level=20,
    )
    defaults.update(overrides)
    return SymbolSpec(**defaults)


def test_resolve_entry_fill_market_when_close_to_current_price():
    series = _series([(0, 4344.0, 4345.0, 4343.0, 4344.5)])
    result = resolve_entry_fill(
        direction="SELL", entry=4344.2, entry_range=None,
        series=series, signal_time=T0, spec=_spec(), max_deviation_pips=100.0,
    )
    assert result is not None
    idx, price, kind = result
    assert kind == "MARKET"
    assert price == 4344.0  # open candle pertama


def test_resolve_entry_fill_pending_stop_fills_later():
    # SELL dengan entry di BAWAH harga sekarang jauh -> SELL_STOP, nunggu breakout turun
    series = _series([
        (0, 4400.0, 4401.0, 4399.0, 4400.5),
        (5, 4398.0, 4399.0, 4396.0, 4397.0),
        (10, 4395.0, 4396.0, 4393.0, 4394.0),  # low 4393 <= target 4394? tunggu, target 4200 jauh
    ])
    result = resolve_entry_fill(
        direction="SELL", entry=4394.5, entry_range=None,
        series=series, signal_time=T0, spec=_spec(), max_deviation_pips=1.0,
    )
    assert result is not None
    idx, price, kind = result
    assert kind == "SELL_STOP"
    assert price == 4394.5
    assert idx == 2  # candle ke-3 (menit 10) low=4393 <= 4394.5


def test_resolve_entry_fill_never_touched_returns_none():
    series = _series([
        (0, 4400.0, 4401.0, 4399.0, 4400.5),
        (5, 4400.0, 4401.0, 4399.5, 4400.0),
    ])
    result = resolve_entry_fill(
        direction="SELL", entry=4000.0, entry_range=None,  # jauh sekali, tidak akan pernah tersentuh
        series=series, signal_time=T0, spec=_spec(), max_deviation_pips=1.0,
    )
    assert result is None


def test_resolve_trade_up_to_sl_hit():
    series = _series([
        (0, 4344.0, 4345.0, 4343.0, 4344.5),
        (5, 4344.0, 4344.5, 4340.0, 4341.0),  # low 4340 <= sl 4341 (SELL: sl di atas entry)
    ])
    trade = SimulatedTrade(
        signal_message_id=1, canonical_symbol="XAUUSD", direction="SELL",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4330.0, kind="MARKET",
    )
    # untuk SELL, sl_hit = candle.high >= sl. Ubah data biar high tembus sl.
    series2 = _series([
        (0, 4344.0, 4345.0, 4343.0, 4344.5),
        (5, 4344.0, 4349.0, 4343.0, 4348.5),  # high 4349 >= sl 4348
    ])
    resolve_trade_up_to(trade, series2, T0 + timedelta(minutes=10))
    assert trade.exit_reason == "sl"
    assert trade.exit_price == 4348.0


def test_resolve_trade_up_to_tp_hit():
    series = _series([
        (0, 4344.0, 4345.0, 4343.0, 4344.5),
        (5, 4344.0, 4344.5, 4330.0, 4331.0),  # low 4330 <= tp 4333 (SELL, TP di bawah entry)
    ])
    trade = SimulatedTrade(
        signal_message_id=2, canonical_symbol="XAUUSD", direction="SELL",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    resolve_trade_up_to(trade, series, T0 + timedelta(minutes=10))
    assert trade.exit_reason == "tp"
    assert trade.exit_price == 4333.0


def test_resolve_trade_up_to_both_hit_same_candle_sl_wins():
    # candle high tembus SL DAN low tembus TP sekaligus -> asumsi konservatif: SL menang
    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.0),
        (5, 4344.0, 4350.0, 4320.0, 4330.0),  # high 4350>=sl 4348, low 4320<=tp 4333
    ])
    trade = SimulatedTrade(
        signal_message_id=3, canonical_symbol="XAUUSD", direction="SELL",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    resolve_trade_up_to(trade, series, T0 + timedelta(minutes=10))
    assert trade.exit_reason == "sl"


def test_resolve_trade_up_to_incremental_sl_change_not_retroactive():
    # candle 1: harga masih jauh dari SL lama maupun baru
    # follow-up terjadi di menit 5 -> SL dipindah ke breakeven (4344.0)
    # candle 2 (menit 10): harga turun tembus SL LAMA (4348) tapi TIDAK tembus SL BARU (4344)
    #   -> karena SL sudah diupdate SEBELUM candle ini di-scan, harusnya TIDAK exit di sini
    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.0),
        (10, 4344.0, 4346.0, 4345.0, 4345.5),  # antara SL baru (4344) dan SL lama (4348), tidak trigger apapun
    ])
    trade = SimulatedTrade(
        signal_message_id=4, canonical_symbol="XAUUSD", direction="SELL",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    # resolve sampai menit 5 (sebelum candle ke-2) -> belum ada apa2
    resolve_trade_up_to(trade, series, T0 + timedelta(minutes=5))
    assert trade.exit_reason is None

    # follow-up: pindah SL ke breakeven
    trade.sl = trade.entry_price
    trade.be_moved = True

    # lanjutkan resolve sampai menit 15 -> candle ke-2 (menit 10) high=4346 < sl_baru? BUKAN,
    # untuk SELL sl_hit = high >= sl. sl baru = 4344.0, high candle = 4346 >= 4344 -> KENA
    resolve_trade_up_to(trade, series, T0 + timedelta(minutes=15))
    assert trade.exit_reason == "sl"
    assert trade.exit_price == 4344.0  # exit di SL BARU (breakeven), bukan SL lama


def test_pnl_usd_sell_profit():
    trade = SimulatedTrade(
        signal_message_id=5, canonical_symbol="XAUUSD", direction="SELL",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    spec = _spec()
    # SELL profit: exit lebih rendah dari entry. price_diff = entry-exit = 4344-4333=11
    result = pnl_usd(trade, exit_price=4333.0, lot=0.1, spec=spec)
    # ticks = 11/0.01=1100, pnl = 1100*1.0*0.1 = 110
    assert abs(result - 110.0) < 1e-6


def test_pnl_usd_buy_loss():
    trade = SimulatedTrade(
        signal_message_id=6, canonical_symbol="XAUUSD", direction="BUY",
        lot=0.1, entry_price=4344.0, entry_time=T0, sl=4340.0, tp=4360.0, kind="MARKET",
    )
    spec = _spec()
    result = pnl_usd(trade, exit_price=4340.0, lot=0.1, spec=spec)
    # BUY loss: price_diff = exit-entry = 4340-4344=-4 -> ticks=-400 -> pnl=-400*1*0.1=-40
    assert abs(result - (-40.0)) < 1e-6
