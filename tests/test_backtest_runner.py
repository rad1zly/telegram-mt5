import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from backtest.engine import SymbolSpec  # noqa: E402
from backtest.price_data import Candle, PriceSeries  # noqa: E402
from backtest.runner import BacktestConfig, build_report, run  # noqa: E402
from src.trading.symbols import SymbolResolver  # noqa: E402

T0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
ALIASES = {"XAUUSD": ["GOLD", "XAUUSD"]}


def _spec():
    return SymbolSpec(
        broker_symbol="XAUUSD+", point=0.01, digits=2,
        trade_tick_size=0.01, trade_tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        trade_stops_level=20,
    )


def _series(rows):
    candles = [
        Candle(time=T0 + timedelta(minutes=m), open=o, high=h, low=lo, close=c)
        for m, o, h, lo, c in rows
    ]
    return PriceSeries(candles)


def _config():
    return BacktestConfig(
        risk_usd=50.0, max_lot_cap=5.0, max_price_deviation_pips=100.0,
        price_deviation_overrides={"XAUUSD": 100.0},
        min_sl_distance_overrides={},
    )


def test_run_end_to_end_market_entry_hits_tp():
    signal_time = T0.isoformat()
    rows = [{
        "message_id": 1,
        "date_utc": signal_time,
        "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
        "reply_to_msg_id": None,
    }]

    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),   # signal time -> market fill ~4344
        (5, 4344.0, 4344.5, 4322.0, 4323.5),   # low tembus TP terakhir (4323)
    ])

    resolver = SymbolResolver(ALIASES)
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=_config(),
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "tp"
    assert trade.direction == "SELL"

    report = build_report(trades, {"XAUUSD": _spec()}, skipped)
    assert report.total_trades == 1
    assert report.closed_trades == 1
    assert report.wins == 1
    assert report.total_pnl_usd > 0


def test_run_skips_signal_when_symbol_not_covered():
    rows = [{
        "message_id": 1,
        "date_utc": T0.isoformat(),
        "text": "EURUSD\n\nsell below 1.0810 - 1.0820\n\ntp.: 1.0780\nsl.: 1.0850",
        "reply_to_msg_id": None,
    }]
    resolver = SymbolResolver({"XAUUSD": ["GOLD"], "EURUSD": ["EURUSD"]})
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": _series([(0, 4344.0, 4344.5, 4343.5, 4344.2)])},
        symbol_specs={"XAUUSD": _spec()},
        config=_config(),
    )
    assert trades == []
    assert skipped["symbol_not_covered"] == 1


def test_run_applies_followup_move_sl_be_and_partial_close():
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            "text": "GOLD | Live Update\n\nYou may close partially to secure gains and move the stop-loss to the entry.",
            "reply_to_msg_id": None,
        },
    ]

    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),   # saat follow-up, harga masih di sekitar sini
        (10, 4344.0, 4345.0, 4343.5, 4344.5),  # naik balik ke entry -> exit di SL(BE) utk sisa lot
    ])

    resolver = SymbolResolver(ALIASES)
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=_config(),
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.be_moved is True
    assert trade.tp1_hit is True
    assert trade.remaining_lot < trade.lot  # sebagian sudah di-partial-close
    assert trade.exit_reason == "sl"  # SL (breakeven) kena di candle menit 10
    assert trade.exit_price == trade.entry_price


def test_run_applies_sl_plus_automatically_even_without_move_sl_be_text():
    # Channel CUMA bilang "close partially" -- TIDAK menyebut pindah SL sama
    # sekali. SL+ (breakeven+buffer) harus tetap terpicu otomatis kalau
    # sl_plus_buffer_overrides diisi di config.
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            "text": "GOLD | Live Update\n\nHit Target +10 pip. You may close partially to secure gains.",
            "reply_to_msg_id": None,
        },
    ]

    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),
    ])

    resolver = SymbolResolver(ALIASES)
    config = BacktestConfig(
        risk_usd=50.0, max_lot_cap=5.0, max_price_deviation_pips=100.0,
        price_deviation_overrides={"XAUUSD": 100.0},
        min_sl_distance_overrides={},
        sl_plus_buffer_overrides={"XAUUSD": 0.4},
    )
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=config,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.tp1_hit is True
    assert trade.be_moved is True
    # SELL -> SL+ di BAWAH entry (mengunci profit), bukan exact breakeven
    assert trade.sl == trade.entry_price - 0.4


def test_run_executes_close_all_when_enabled():
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            "text": "GOLD | Live Update\n\nHit Profit +50 pip.\n\nWe now prefer to close the position due to volatility.",
            "reply_to_msg_id": None,
        },
    ]
    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),
        (10, 4344.0, 4345.0, 4343.5, 4344.5),  # kalau close_all TIDAK jalan, ini akan lanjut ke SL/TP
    ])

    resolver = SymbolResolver(ALIASES)
    config = BacktestConfig(
        risk_usd=50.0, max_lot_cap=5.0, max_price_deviation_pips=100.0,
        price_deviation_overrides={"XAUUSD": 100.0},
        min_sl_distance_overrides={},
        close_all_enabled=True,
    )
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=config,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "close_all"
    assert trade.exit_time == T0 + timedelta(minutes=5)  # closed persis saat follow-up, bukan nunggu SL/TP
    assert trade.remaining_lot == 0.0


def test_run_close_all_kind_ignored_when_disabled():
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            "text": "GOLD | Live Update\n\nHit Profit +50 pip.\n\nWe now prefer to close the position due to volatility.",
            "reply_to_msg_id": None,
        },
    ]
    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),
        (10, 4344.0, 4344.5, 4322.0, 4323.5),  # tembus TP terakhir (4323)
    ])

    resolver = SymbolResolver(ALIASES)
    config = _config()  # close_all_enabled default False
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=config,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "tp"  # tetap jalan sampai TP, close_all diabaikan
