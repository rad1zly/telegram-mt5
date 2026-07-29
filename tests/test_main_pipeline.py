import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, ".")

import src.main as main_mod  # noqa: E402
from src.store.db import Database  # noqa: E402
from src.tg import notifier  # noqa: E402
from src.trading import mt5_client  # noqa: E402
from src.trading.executor import ExecutionResult  # noqa: E402
from src.trading.symbols import SymbolResolver  # noqa: E402

GOLD_ENTRY_TEXT = "GOLD\n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348"
US30_LIVE_UPDATE_TEXT = (
    "US30 | Live Update\n\nYou may close partially to secure gains "
    "and move the stop-loss to the entry."
)
CHATTER_TEXT = "selamat pagi semua, semoga profit hari ini"


def _fake_msg(msg_id, text, reply_to_msg_id=None):
    return SimpleNamespace(
        id=msg_id,
        raw_text=text,
        date=datetime.now(timezone.utc),
        reply_to_msg_id=reply_to_msg_id,
        to_dict=lambda: {"id": msg_id},
    )


def _make_ctx(tmp_path, **settings_overrides):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    resolver = SymbolResolver({"XAUUSD": ["GOLD", "XAUUSD"], "US30": ["US30"]})
    settings = {
        "risk": {"usd_per_trade": 50.0, "max_lot_cap": 5.0, "max_trades_per_day": 20},
        "followup": {"move_sl_to_be": True, "partial_close_tp1": True, "partial_close_percent": 50, "close_all": False},
        "guards": {"max_price_deviation_pips": 15.0, "max_spread_pips": 30.0},
    }
    for key, value in settings_overrides.items():
        settings[key].update(value)
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


def test_followup_applies_move_sl_be_and_partial_close(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    notified = []
    monkeypatch.setattr(notifier, "send", lambda text: notified.append(text))

    # posisi terbuka yang jadi target follow-up
    ctx.db.insert_position({
        "signal_id": 10, "ticket": 555, "symbol": "US30", "lot": 0.2,
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

    assert len(modify_calls) == 1
    assert modify_calls[0] == (555, "US30", 48500.0)
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
        "signal_id": 10, "ticket": 555, "symbol": "US30", "lot": 0.2,
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
        "signal_id": 1, "ticket": 1, "symbol": "GOLD", "lot": 0.1,
        "open_price": 4344.5, "sl": 4348.0, "tp": 4333.0,
        "status": "open", "opened_at": main_mod.today_start_iso(),
    })

    msg = _fake_msg(20, GOLD_ENTRY_TEXT)
    asyncio.run(main_mod.handle_new_message(ctx, "chan", msg))

    assert execute_calls == []  # tidak dieksekusi karena sudah kena limit
    assert any("dilewati" in n for n in notified)
