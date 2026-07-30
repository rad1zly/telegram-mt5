import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from backtest.engine import SimulatedTrade, SymbolSpec  # noqa: E402
from backtest.tick_data import TickSeries, _to_ns  # noqa: E402
from backtest.tick_engine import resolve_entry_fill_tick, resolve_trade_up_to_tick  # noqa: E402

T0 = datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc)


def _spec():
    return SymbolSpec(
        broker_symbol="XAUUSD+", point=0.01, digits=2,
        trade_tick_size=0.01, trade_tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        trade_stops_level=20,
    )


def _series(rows):
    # rows: list of (seconds_offset, bid, ask)
    times = np.array([_to_ns(T0 + timedelta(seconds=s)) for s, _, _ in rows], dtype="int64")
    bids = np.array([b for _, b, _ in rows], dtype="float64")
    asks = np.array([a for _, _, a in rows], dtype="float64")
    return TickSeries(times=times, bids=bids, asks=asks)


def test_resolve_entry_fill_market_order_when_within_tolerance():
    series = _series([(0, 4344.0, 4344.2)])
    fill = resolve_entry_fill_tick(
        direction="SELL", entry=4344.1, entry_range=None,
        tick_series=series, signal_time=T0, spec=_spec(), max_deviation_pips=100.0,
    )
    assert fill is not None
    idx, price, kind = fill
    assert kind == "MARKET"
    assert price == 4344.0  # SELL market pakai BID


def test_resolve_entry_fill_pending_sell_stop_waits_for_bid_to_drop():
    series = _series([
        (0, 4400.0, 4400.2),
        (5, 4390.0, 4390.2),
        (10, 4344.0, 4344.2),  # bid akhirnya turun ke level target
    ])
    fill = resolve_entry_fill_tick(
        direction="SELL", entry=4344.0, entry_range=None,
        tick_series=series, signal_time=T0, spec=_spec(), max_deviation_pips=1.0,
    )
    assert fill is not None
    idx, price, kind = fill
    assert kind == "SELL_STOP"
    assert idx == 2
    assert price == 4344.0


def test_resolve_entry_fill_returns_none_when_pending_never_touched():
    series = _series([(0, 4400.0, 4400.2), (5, 4390.0, 4390.2)])
    fill = resolve_entry_fill_tick(
        direction="SELL", entry=4344.0, entry_range=None,
        tick_series=series, signal_time=T0, spec=_spec(), max_deviation_pips=1.0,
    )
    assert fill is None


def test_resolve_trade_up_to_tick_hits_tp_before_sl_when_price_moves_favorably_first():
    trade = SimulatedTrade(
        signal_message_id=1, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4333.7, 4333.9),
        (10, 4332.8, 4333.0),  # ASK nyentuh TP (4333) -- posisi SELL ditutup dgn ASK
        (15, 4348.5, 4348.7),  # baru naik ke SL SETELAHNYA -- tidak relevan lagi
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=20))
    assert trade.exit_reason == "tp"
    assert trade.exit_price == 4333.0


def test_resolve_trade_up_to_tick_hits_sl_when_that_comes_first():
    trade = SimulatedTrade(
        signal_message_id=2, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4348.0, 4348.2),  # ASK nyentuh SL duluan
        (10, 4333.0, 4333.2),  # baru turun ke TP -- sudah tidak relevan
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=20))
    assert trade.exit_reason == "sl"
    assert trade.exit_price == 4348.0


def test_resolve_trade_up_to_tick_buy_position_uses_bid_to_close():
    trade = SimulatedTrade(
        signal_message_id=3, canonical_symbol="XAUUSD", direction="BUY", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4338.0, tp=4355.0, kind="MARKET",
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4354.8, 4355.0),  # BID belum nyentuh TP (4355) -- ASK sudah tapi bid yg relevan
        (10, 4355.0, 4355.2),  # sekarang BID nyentuh TP
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=20))
    assert trade.exit_reason == "tp"
    assert trade._last_resolved_index == 2


def test_auto_be_r_multiple_moves_sl_to_entry_mechanically():
    trade = SimulatedTrade(
        signal_message_id=4, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4300.0, kind="MARKET", r_value=4.0,
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4339.8, 4340.0),  # ASK turun ke 4340.0 -- persis 1R (4344-4340=4) -- auto-BE trigger
        (10, 4344.5, 4344.7),  # lalu balik naik dikit, TAPI sl sudah di entry (4344) -- kena SL(BE)
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=20), auto_be_r_multiple=1.0)
    assert trade.be_moved is True
    assert trade.exit_reason == "sl"
    assert trade.exit_price == trade.entry_price


def test_auto_be_does_not_reapply_old_sl_after_moving_to_breakeven():
    # SL LAMA (4348) TIDAK boleh lagi jadi acuan setelah auto-BE mindahin
    # SL ke entry (4344) -- fase 2 harus pakai SL BARU, bukan lama.
    trade = SimulatedTrade(
        signal_message_id=5, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4300.0, kind="MARKET", r_value=4.0,
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4339.8, 4340.0),  # auto-BE trigger (1R)
        (10, 4310.0, 4310.2),  # lanjut turun mendekati TP, TIDAK pernah balik ke 4348
        (15, 4299.8, 4300.0),  # ASK nyentuh TP (4300)
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=20), auto_be_r_multiple=1.0)
    assert trade.be_moved is True
    assert trade.exit_reason == "tp"
    assert trade.exit_price == 4300.0


def test_resolve_trade_up_to_tick_respects_up_to_time_boundary():
    # Belum ada tick baru yg diproses melebihi up_to_time -- trade tetap
    # terbuka, TIDAK exit walau tick SETELAH up_to_time sebenarnya kena SL.
    trade = SimulatedTrade(
        signal_message_id=6, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=T0, sl=4348.0, tp=4300.0, kind="MARKET",
    )
    series = _series([
        (0, 4344.0, 4344.2),
        (5, 4344.5, 4344.7),
        (100, 4348.5, 4348.7),  # ini SETELAH up_to_time -- belum boleh diproses
    ])
    resolve_trade_up_to_tick(trade, series, T0 + timedelta(seconds=10))
    assert trade.exit_reason is None
    assert trade.is_open is True
