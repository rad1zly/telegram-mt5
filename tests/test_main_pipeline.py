import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")

import src.main as main_mod  # noqa: E402
from src.store.db import Database  # noqa: E402
from src.tg import notifier  # noqa: E402
from src.trading import mt5_client  # noqa: E402
from src.trading.executor import ExecutionResult  # noqa: E402
from src.trading.symbols import SymbolResolver  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_llm_calls(monkeypatch):
    # Test suite TIDAK BOLEH bergantung pada ada-tidaknya MINIMAX_API_KEY
    # sungguhan di environment developer (config/.env) -- kalau kebetulan
    # terisi (mis. lagi disiapkan utk trial live), test yang mengandalkan
    # llm_available()==False bisa diam-diam memanggil API sungguhan (lambat,
    # berbayar, non-deterministik). Default aman: anggap LLM tidak tersedia,
    # kecuali test tertentu SENGAJA override balik utk menguji jalur LLM.
    monkeypatch.setattr(main_mod, "llm_available", lambda: False)

GOLD_ENTRY_TEXT = "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348"
US30_LIVE_UPDATE_TEXT = (
    "US30 | Live Update\n\nYou may close partially "
    "and move the stop-loss to the entry."
)
CHATTER_TEXT = "selamat pagi semua, semoga profit hari ini"
GOLD_PARTIAL_CLOSE_ONLY_TEXT = "GOLD | Live Update\n\nYou may close partially."
GOLD_CLOSE_ALL_TEXT = "GOLD | Live Update\n\nHit Profit +110 pip.\n\nWe now prefer to close the position due to the current geopolitical situation."


def _fake_msg(msg_id, text, reply_to_msg_id=None, date=None):
    return SimpleNamespace(
        id=msg_id,
        raw_text=text,
        date=date if date is not None else datetime.now(timezone.utc),
        reply_to_msg_id=reply_to_msg_id,
        to_dict=lambda: {"id": msg_id},
    )


def _make_ctx(tmp_path, **settings_overrides):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    resolver = SymbolResolver({"XAUUSD": ["GOLD", "XAUUSD"], "US30": ["US30"]})
    settings = {
        "risk": {"usd_per_trade": 50.0, "max_lot_cap": 5.0, "max_trades_per_day": 20},
        "followup": {
            "move_sl_to_be": True, "partial_close_tp1": True, "partial_close_percent": 50, "close_all": False,
            "sl_plus_buffer_overrides": {"US30": 4.2, "XAUUSD": 0.4},
        },
        "guards": {"max_price_deviation_pips": 15.0, "max_spread_pips": 30.0},
    }
    for key, value in settings_overrides.items():
        settings.setdefault(key, {}).update(value)
    return main_mod.Context(settings=settings, db=db, resolver=resolver, broker_symbols=["XAUUSD", "US30"])


def _fake_symbol_info():
    return SimpleNamespace(
        trade_tick_size=0.01, trade_tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
    )


def test_entry_signal_executed_and_notified(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())
    monkeypatch.setattr(
        main_mod,
        "execute_signal",
        lambda **kw: ExecutionResult(success=True, detail="Order sukses: MARKET SELL XAUUSD lot=0.1", ticket=111, lot=0.1, price=4344.5),
    )

    msg = _fake_msg(1, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert len(notified) == 1
    assert "Order sukses" in notified[0] or "✅" in notified[0]

    position = ctx.db.get_open_position_by_symbol("GOLD")
    assert position is not None
    assert position["ticket"] == 111


def test_duplicate_message_processed_only_once(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())
    monkeypatch.setattr(
        main_mod, "execute_signal",
        lambda **kw: ExecutionResult(success=True, detail="ok", ticket=1, lot=0.1, price=4344.5),
    )

    msg = _fake_msg(2, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert len(notified) == 1


def test_unrecognized_chatter_is_ignored_without_crash(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    msg = _fake_msg(3, CHATTER_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert notified == []


class TestLLMFirstMode:
    """parser.llm_first: true -> classify_message_with_llm jadi pengambil
    keputusan UTAMA (bukan regex), lihat classify_and_act. Revert cukup set
    llm_first: false di config, tanpa ubah kode -- diverifikasi test kedua."""

    def test_llm_first_routes_signal_to_entry_handler(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, parser={"llm_first": True})
        monkeypatch.setattr(main_mod, "llm_available", lambda: True)

        from src.parser.schema import Signal
        fake_signal = Signal(message_id=20, action="SELL", symbol="GOLD", entry=4344.5, sl=4348.0, tp=[4333.0])
        monkeypatch.setattr(main_mod, "classify_message_with_llm", lambda *a, **kw: fake_signal)

        notified = []
        monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
        monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())
        monkeypatch.setattr(
            main_mod, "execute_signal",
            lambda **kw: ExecutionResult(success=True, detail="ok", ticket=99, lot=0.1, price=4344.5),
        )

        msg = _fake_msg(20, "teks apa pun, keputusan sepenuhnya dari LLM yg di-mock")
        asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

        position = ctx.db.get_open_position_by_symbol("GOLD")
        assert position is not None
        assert position["ticket"] == 99

    def test_llm_first_routes_followup_to_followup_handler(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, parser={"llm_first": True})
        monkeypatch.setattr(main_mod, "llm_available", lambda: True)

        from src.parser.schema import FollowUp
        fake_followup = FollowUp(message_id=21, reply_to_msg_id=None, kinds=["move_sl_be"], raw_text="x", symbol="GOLD")
        monkeypatch.setattr(main_mod, "classify_message_with_llm", lambda *a, **kw: fake_followup)

        notified = []
        monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

        ctx.db.insert_position({
            "signal_id": 1, "ticket": 50, "symbol": "GOLD", "direction": "SELL", "lot": 0.1,
            "open_price": 4344.5, "sl": 4348.0, "tp": 4333.0,
            "status": "open", "opened_at": "2025-01-01T00:00:00+00:00",
        })
        monkeypatch.setattr(mt5_client, "get_position", lambda ticket: SimpleNamespace(volume=0.1))
        monkeypatch.setattr(
            mt5_client, "modify_sl_tp",
            lambda ticket, symbol, sl=None, tp=None: SimpleNamespace(success=True, error=None),
        )

        msg = _fake_msg(21, "teks apa pun, keputusan sepenuhnya dari LLM yg di-mock")
        asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

        assert any("breakeven" in n for n in notified)

    def test_llm_first_no_tool_call_means_no_action(self, tmp_path, monkeypatch):
        ctx = _make_ctx(tmp_path, parser={"llm_first": True})
        monkeypatch.setattr(main_mod, "llm_available", lambda: True)
        monkeypatch.setattr(main_mod, "classify_message_with_llm", lambda *a, **kw: None)

        notified = []
        monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

        msg = _fake_msg(22, CHATTER_TEXT)
        asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

        assert notified == []

    def test_llm_first_disabled_falls_back_to_regex_path(self, tmp_path, monkeypatch):
        # parser.llm_first ABSEN dari settings (default) -> harus tetap
        # pakai regex, classify_message_with_llm TIDAK boleh dipanggil (regex
        # fallback lama -- parse_signal_with_llm/parse_followup_with_llm --
        # di-mock juga di sini supaya test ini tidak diam-diam manggil API
        # sungguhan lewat jalur fallback lama).
        ctx = _make_ctx(tmp_path)
        monkeypatch.setattr(main_mod, "llm_available", lambda: True)

        called = []
        monkeypatch.setattr(main_mod, "classify_message_with_llm", lambda *a, **kw: called.append(1))
        monkeypatch.setattr(main_mod, "parse_signal_with_llm", lambda *a, **kw: None)
        monkeypatch.setattr(main_mod, "parse_followup_with_llm", lambda *a, **kw: None)

        notified = []
        monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

        msg = _fake_msg(23, CHATTER_TEXT)
        asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

        assert called == []
        assert notified == []


def test_followup_applies_move_sl_be_and_partial_close(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    # posisi terbuka yang jadi target follow-up
    ctx.db.insert_position({
        "signal_id": 10, "ticket": 555, "symbol": "US30", "direction": "SELL", "lot": 0.2,
        "open_price": 48500.0, "sl": 48560.0, "tp": 48420.0,
        "status": "open", "opened_at": "2026-07-28T09:00:00+00:00",
    })

    # posisi masih hidup di broker (bukan stale)
    monkeypatch.setattr(mt5_client, "get_position", lambda ticket: SimpleNamespace(ticket=ticket))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    modify_calls = []
    partial_calls = []
    monkeypatch.setattr(mt5_client, "modify_sl_tp", lambda ticket, symbol, sl=None, tp=None: (
        modify_calls.append((ticket, symbol, sl)),
        SimpleNamespace(success=True),
    )[1])
    monkeypatch.setattr(mt5_client, "partial_close", lambda ticket, symbol, volume: (
        partial_calls.append((ticket, symbol, volume)),
        SimpleNamespace(success=True),
    )[1])

    msg = _fake_msg(11, US30_LIVE_UPDATE_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    # move_sl_be (exact breakeven) dulu, LALU SL+ otomatis (breakeven+buffer)
    # meng-override lagi -- SL akhir harus SL+, bukan exact breakeven.
    assert len(modify_calls) == 2
    assert modify_calls[0] == (555, "US30", 48500.0)
    assert modify_calls[1] == (555, "US30", 48500.0 - 4.2)  # SELL -> SL+ di BAWAH entry
    assert len(partial_calls) == 1
    assert partial_calls[0] == (555, "US30", 0.1)  # 50% dari lot 0.2, step 0.01

    position = ctx.db.get_open_position_by_symbol("US30")
    assert position["be_moved"] == 1
    assert position["tp1_hit"] == 1


def test_followup_skips_stale_position_and_notifies(tmp_path, monkeypatch):
    # posisi tercatat 'open' di DB lokal tapi sudah tidak ada di broker
    # (kena TP/SL) -> harus disinkronkan ke closed, bukan dipaksa modifikasi
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    ctx.db.insert_position({
        "signal_id": 10, "ticket": 555, "symbol": "US30", "direction": "SELL", "lot": 0.2,
        "open_price": 48500.0, "sl": 48560.0, "tp": 48420.0,
        "status": "open", "opened_at": "2026-07-28T09:00:00+00:00",
    })
    monkeypatch.setattr(mt5_client, "get_position", lambda ticket: None)  # sudah tidak ada di broker

    msg = _fake_msg(12, US30_LIVE_UPDATE_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert any("tidak ada posisi terbuka" in n for n in notified)
    assert ctx.db.get_open_position_by_symbol("US30") is None  # sudah disinkronkan ke closed


def test_edited_message_after_execution_does_not_reexecute(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    execute_calls = []
    monkeypatch.setattr(
        main_mod, "execute_signal",
        lambda **kw: (execute_calls.append(1), ExecutionResult(success=True, detail="ok", ticket=999, lot=0.1, price=4344.5))[1],
    )

    msg = _fake_msg(30, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))
    assert len(execute_calls) == 1  # eksekusi pertama, normal

    # channel edit pesan yang SAMA (typo SL dikoreksi) -> teks beda tapi tetap entry
    edited_msg = _fake_msg(30, "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4350")
    asyncio.run(main_mod.handle_edited_message(ctx, "chan", edited_msg))

    assert len(execute_calls) == 1  # TIDAK eksekusi kedua kalinya
    assert any("DIEDIT channel SETELAH" in n for n in notified)


def test_edited_message_not_previously_seen_is_processed_as_new(tmp_path, monkeypatch):
    # bot baru start setelah edit terjadi -> message_id belum pernah tercatat
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())
    monkeypatch.setattr(
        main_mod, "execute_signal",
        lambda **kw: ExecutionResult(success=True, detail="ok", ticket=1, lot=0.1, price=4344.5),
    )

    msg = _fake_msg(31, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_edited_message(ctx, "chan", msg))

    assert ctx.db.get_open_position_by_symbol("GOLD") is not None


def test_edited_message_of_old_never_seen_message_is_not_executed(tmp_path, monkeypatch):
    # Kasus nyata yang dilaporkan user: bot tiba-tiba buka posisi padahal
    # TIDAK ADA sinyal baru sama sekali yang terlihat di channel. Root
    # cause: event MessageEdited bisa datang untuk pesan LAMA (dikirim
    # hari-hari sebelumnya, sebelum bot pernah mencatatnya) -- teks entry
    # aslinya masih match pola sinyal, jadi tanpa guard ini bot eksekusi
    # ulang seolah sinyal baru.
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    execute_calls = []
    monkeypatch.setattr(main_mod, "execute_signal", lambda **kw: execute_calls.append(1))

    old_date = datetime.now(timezone.utc) - timedelta(days=3)
    msg = _fake_msg(40, GOLD_ENTRY_TEXT, date=old_date)
    asyncio.run(main_mod.handle_edited_message(ctx, "chan", msg))

    assert execute_calls == []
    assert ctx.db.get_open_position_by_symbol("GOLD") is None
    assert any("BUKAN hari ini" in n for n in notified)


def test_edited_message_of_old_tracked_chatter_becoming_signal_is_not_executed(tmp_path, monkeypatch):
    # Pesan lama SUDAH tercatat (dulu bukan sinyal, mis. chatter), lalu
    # hari ini di-edit sampai teksnya berubah jadi cocok pola entry --
    # tetap harus ditolak karena pesan aslinya bukan dari hari ini.
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    execute_calls = []
    monkeypatch.setattr(main_mod, "execute_signal", lambda **kw: execute_calls.append(1))

    old_date = datetime.now(timezone.utc) - timedelta(days=2)
    original_msg = _fake_msg(41, CHATTER_TEXT, date=old_date)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", original_msg))

    edited_msg = _fake_msg(41, GOLD_ENTRY_TEXT, date=old_date)
    asyncio.run(main_mod.handle_edited_message(ctx, "chan", edited_msg))

    assert execute_calls == []
    assert ctx.db.get_open_position_by_symbol("GOLD") is None
    assert any("BUKAN hari ini" in n for n in notified)


def test_edited_message_from_today_never_seen_still_executes(tmp_path, monkeypatch):
    # Beda dgn test di atas: kalau pesan ASLINYA dari HARI INI (mis. bot
    # sempat offline sebentar dan baru menangkap event edit), tetap harus
    # diproses normal sebagai sinyal baru -- guard ini cuma menahan pesan
    # LAMA, bukan menahan semua pesan yang belum pernah tercatat.
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())
    monkeypatch.setattr(
        main_mod, "execute_signal",
        lambda **kw: ExecutionResult(success=True, detail="ok", ticket=77, lot=0.1, price=4344.5),
    )

    msg = _fake_msg(42, GOLD_ENTRY_TEXT, date=main_mod.today_start() + timedelta(hours=1))
    asyncio.run(main_mod.handle_edited_message(ctx, "chan", msg))

    assert ctx.db.get_open_position_by_symbol("GOLD") is not None


def test_max_trades_per_day_guard_skips_new_entry(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, risk={"max_trades_per_day": 1})
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    execute_calls = []
    monkeypatch.setattr(
        main_mod, "execute_signal",
        lambda **kw: (execute_calls.append(1), ExecutionResult(success=True, detail="ok", ticket=1, lot=0.1, price=4344.5))[1],
    )

    # sudah ada 1 posisi dibuka hari ini
    ctx.db.insert_position({
        "signal_id": 1, "ticket": 1, "symbol": "GOLD", "direction": "SELL", "lot": 0.1,
        "open_price": 4344.5, "sl": 4348.0, "tp": 4333.0,
        "status": "open", "opened_at": main_mod.today_start_iso(),
    })

    msg = _fake_msg(20, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert execute_calls == []  # tidak dieksekusi karena sudah kena limit
    assert any("dilewati" in n for n in notified)


def test_sl_plus_applies_automatically_without_explicit_move_sl_be_text(tmp_path, monkeypatch):
    # Channel CUMA bilang "close partially" -- TIDAK menyebut "pindah SL"
    # sama sekali. SL+ harus tetap jalan otomatis (aturan risk management
    # kita sendiri, bukan menunggu instruksi channel).
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    ctx.db.insert_position({
        "signal_id": 10, "ticket": 777, "symbol": "GOLD", "direction": "BUY", "lot": 0.2,
        "open_price": 4020.0, "sl": 4010.0, "tp": 4040.0,
        "status": "open", "opened_at": "2026-07-28T09:00:00+00:00",
    })
    monkeypatch.setattr(mt5_client, "get_position", lambda ticket: SimpleNamespace(ticket=ticket))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    modify_calls = []
    monkeypatch.setattr(mt5_client, "modify_sl_tp", lambda ticket, symbol, sl=None, tp=None: (
        modify_calls.append((ticket, symbol, sl)),
        SimpleNamespace(success=True),
    )[1])
    monkeypatch.setattr(
        mt5_client, "partial_close",
        lambda ticket, symbol, volume: SimpleNamespace(success=True),
    )

    msg = _fake_msg(12, GOLD_PARTIAL_CLOSE_ONLY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    # tidak ada move_sl_be di kinds, tapi SL+ tetap harus terpicu 1x
    # (modify_sl_tp dipanggil dgn broker_symbol hasil resolve, "XAUUSD")
    assert len(modify_calls) == 1
    assert modify_calls[0] == (777, "XAUUSD", 4020.0 + 0.4)  # BUY -> SL+ di ATAS entry

    position = ctx.db.get_open_position_by_symbol("GOLD")
    assert position["be_moved"] == 1


def test_close_all_executes_when_enabled_in_config(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, followup={"close_all": True})
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    ctx.db.insert_position({
        "signal_id": 10, "ticket": 888, "symbol": "GOLD", "direction": "BUY", "lot": 0.2,
        "open_price": 4020.0, "sl": 4010.0, "tp": 4040.0,
        "status": "open", "opened_at": "2026-07-28T09:00:00+00:00",
    })
    # get_position dipanggil 2x: sekali di _find_live_position (verifikasi hidup),
    # sekali lagi di blok close_all (ambil volume terkini dari broker)
    monkeypatch.setattr(mt5_client, "get_position", lambda ticket: SimpleNamespace(ticket=ticket, volume=0.2))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    close_calls = []
    monkeypatch.setattr(mt5_client, "partial_close", lambda ticket, symbol, volume: (
        close_calls.append((ticket, symbol, volume)),
        SimpleNamespace(success=True),
    )[1])

    msg = _fake_msg(13, GOLD_CLOSE_ALL_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert len(close_calls) == 1
    assert close_calls[0] == (888, "XAUUSD", 0.2)
    assert any("ditutup PENUH" in n for n in notified)
    assert ctx.db.get_open_position_by_symbol("GOLD") is None  # sudah closed di DB lokal


def test_close_all_stays_notify_only_when_disabled_in_config(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path, followup={"close_all": False})
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    ctx.db.insert_position({
        "signal_id": 10, "ticket": 889, "symbol": "GOLD", "direction": "BUY", "lot": 0.2,
        "open_price": 4020.0, "sl": 4010.0, "tp": 4040.0,
        "status": "open", "opened_at": "2026-07-28T09:00:00+00:00",
    })
    monkeypatch.setattr(mt5_client, "get_position", lambda ticket: SimpleNamespace(ticket=ticket, volume=0.2))
    monkeypatch.setattr(mt5_client, "get_symbol_info", lambda s: _fake_symbol_info())

    close_calls = []
    monkeypatch.setattr(mt5_client, "partial_close", lambda ticket, symbol, volume: (
        close_calls.append((ticket, symbol, volume)),
        SimpleNamespace(success=True),
    )[1])

    msg = _fake_msg(14, GOLD_CLOSE_ALL_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert close_calls == []  # TIDAK dieksekusi karena close_all off
    assert any("TIDAK dieksekusi otomatis" in n for n in notified)
    assert ctx.db.get_open_position_by_symbol("GOLD") is not None  # masih open
