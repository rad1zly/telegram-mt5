import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, ".")

from backtest.engine import SimulatedTrade, SymbolSpec  # noqa: E402
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


def _closed_trade(pnl: float, exit_time: datetime, msg_id: int) -> SimulatedTrade:
    t = SimulatedTrade(
        signal_message_id=msg_id, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=exit_time - timedelta(minutes=5), sl=4348.0, tp=4333.0, kind="MARKET",
    )
    t.exit_price = 4340.0
    t.exit_time = exit_time
    t.exit_reason = "tp" if pnl > 0 else "sl"
    t.remaining_lot = 0.0  # pnl full sudah di realized_pnl_usd, tidak perlu hitung ulang lewat pnl_usd()
    t.realized_pnl_usd = pnl
    return t


def test_build_report_computes_max_drawdown_consecutive_loss_and_profit_factor():
    # Urutan KRONOLOGIS (exit_time): +100, -50, -30, -20, +200, -10
    # equity kumulatif : 100, 50, 20, 0, 200, 190
    # peak berjalan     : 100, 100, 100, 100, 200, 200
    # drawdown per titik: 0,   50,  80,  100, 0,   10   -> max_dd = 100
    # run loss beruntun terpanjang: -50,-30,-20 (3x) sebelum +200 me-reset
    # profit factor: gross_profit=300 (100+200), gross_loss=110 (50+30+20+10) -> 300/110
    pnls = [100.0, -50.0, -30.0, -20.0, 200.0, -10.0]
    trades = [
        _closed_trade(pnl, T0 + timedelta(minutes=i * 10), msg_id=i)
        for i, pnl in enumerate(pnls)
    ]
    # sengaja diacak urutannya di list -- build_report harus sort sendiri pakai exit_time
    shuffled = [trades[3], trades[0], trades[5], trades[1], trades[4], trades[2]]

    report = build_report(shuffled, {"XAUUSD": _spec()}, skipped={})

    assert report.max_drawdown_usd == pytest.approx(100.0)
    assert report.max_consecutive_losses == 3
    assert report.profit_factor == pytest.approx(300.0 / 110.0)


def test_build_report_profit_factor_is_none_when_no_losses():
    trades = [_closed_trade(50.0, T0, msg_id=1), _closed_trade(30.0, T0 + timedelta(minutes=5), msg_id=2)]
    report = build_report(trades, {"XAUUSD": _spec()}, skipped={})
    assert report.profit_factor is None
    assert report.max_drawdown_usd == pytest.approx(0.0)
    assert report.max_consecutive_losses == 0


def _closed_trade_at(entry_time: datetime, exit_time: datetime, pnl: float, msg_id: int) -> SimulatedTrade:
    t = SimulatedTrade(
        signal_message_id=msg_id, canonical_symbol="XAUUSD", direction="SELL", lot=0.1,
        entry_price=4344.0, entry_time=entry_time, sl=4348.0, tp=4333.0, kind="MARKET",
    )
    t.exit_price = 4340.0
    t.exit_time = exit_time
    t.exit_reason = "tp" if pnl > 0 else "sl"
    t.remaining_lot = 0.0
    t.realized_pnl_usd = pnl
    return t


def test_build_report_balance_and_equity_drawdown_pct_match_when_no_overlap():
    # Tidak ada trade yang tumpang tindih -> equity dan balance harus
    # persis sama sepanjang waktu (tidak ada floating dari posisi LAIN).
    trade = _closed_trade_at(T0, T0 + timedelta(minutes=10), -160.0, msg_id=1)
    report = build_report([trade], {"XAUUSD": _spec()}, skipped={}, initial_deposit=800.0)
    assert report.max_balance_drawdown_pct == pytest.approx(20.0)  # 160/800
    assert report.max_equity_drawdown_pct == pytest.approx(20.0)


def test_build_report_balance_vs_equity_drawdown_pct_diverge_on_overlapping_trades():
    # Trade A (rugi besar -400) masih terbuka saat Trade B (untung +100)
    # dibuka & ditutup -- equity harus mencerminkan floating rugi A itu
    # SEBELUM A benar-benar closed, balance TIDAK (baru berubah pas close).
    # A: entry T0, exit T0+100m, pnl -400
    # B: entry T0+50m, exit T0+150m, pnl +100
    # balance: 800 -> (t=100) 400 -> (t=150) 500 => max DD balance = 400/800 = 50%
    # equity : 800 -> (t=50, A floating -200) 600 -> (t=100, A closed+B floating +50) 450
    #          -> (t=150) 500 => max DD equity = (800-450)/800 = 43.75%
    trade_a = _closed_trade_at(T0, T0 + timedelta(minutes=100), -400.0, msg_id=1)
    trade_b = _closed_trade_at(T0 + timedelta(minutes=50), T0 + timedelta(minutes=150), 100.0, msg_id=2)

    report = build_report([trade_a, trade_b], {"XAUUSD": _spec()}, skipped={}, initial_deposit=800.0)

    assert report.max_balance_drawdown_pct == pytest.approx(50.0)
    assert report.max_equity_drawdown_pct == pytest.approx(43.75)


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
            "text": "GOLD | Live Update\n\nYou may close partially and move the stop-loss to the entry.",
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
            "text": "GOLD | Live Update\n\nYou may close partially.",
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


def test_run_resolves_followup_via_reply_chain_without_symbol_in_text():
    # Kasus nyata (msg 2096->2097->2098 di korpus asli): follow-up TIDAK
    # menyebut simbol sama sekali di teksnya (simbol cuma disebut di pesan
    # BERANTAI sebelumnya) -- follow-up_symbol utk parser jadi None, TAPI
    # reply_to_msg_id-nya menunjuk balik ke entry -> harus tetap ke-resolve
    # lewat reply chain, bukan gagal karena followup.symbol kosong.
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=3)).isoformat(),
            "text": "GOLD | Live Update\n\nAlready touched the level of 4340.",
            "reply_to_msg_id": 1,  # reply ke entry
        },
        {
            "message_id": 3, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            # TIDAK ada nama simbol sama sekali di sini (persis kasus nyata
            # msg 2098) -- reply_to_msg_id menunjuk ke pesan 2 (BUKAN
            # langsung ke entry), jadi harus telusuri 2 hop: 3->2->1 lewat
            # trade_by_message_id (pesan 2 sudah ter-resolve ke trade yg
            # sama sebagai efek samping saat diproses).
            "text": "You may close partially and move the stop-loss to the entry.",
            "reply_to_msg_id": 2,
        },
    ]

    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),
        (10, 4344.0, 4345.0, 4343.5, 4344.5),
    ])

    resolver = SymbolResolver(ALIASES)
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=_config(),
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.tp1_hit is True  # partial close berhasil dieksekusi via reply chain
    assert trade.be_moved is True


def test_run_reply_chain_falls_back_to_symbol_matching_when_no_reply():
    # Follow-up TANPA reply_to_msg_id (None) dan tanpa chain yang valid ->
    # harus tetap jalan lewat fallback lama (simbol+waktu-terdekat), bukan
    # gagal total.
    rows = [
        {
            "message_id": 1, "date_utc": T0.isoformat(),
            "text": "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348",
            "reply_to_msg_id": None,
        },
        {
            "message_id": 2, "date_utc": (T0 + timedelta(minutes=5)).isoformat(),
            "text": "GOLD | Live Update\n\nYou may close partially and move the stop-loss to the entry.",
            "reply_to_msg_id": None,
        },
    ]
    series = _series([
        (0, 4344.0, 4344.5, 4343.5, 4344.2),
        (5, 4340.0, 4340.5, 4339.5, 4340.0),
        (10, 4344.0, 4345.0, 4343.5, 4344.5),
    ])
    resolver = SymbolResolver(ALIASES)
    trades, skipped = run(
        signal_rows=rows, resolver=resolver, broker_symbols=["XAUUSD+"],
        price_series={"XAUUSD": series}, symbol_specs={"XAUUSD": _spec()},
        config=_config(),
    )
    assert len(trades) == 1
    assert trades[0].tp1_hit is True
