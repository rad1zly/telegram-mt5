import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from src.parser.followup import parse_followup_regex  # noqa: E402
from src.parser.patterns import parse_entry_signal  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "signals.jsonl"

if not FIXTURE_PATH.exists():
    pytest.skip(
        "tests/fixtures/signals.jsonl tidak ada — file ini gitignored karena "
        "berisi data channel signal privat/berbayar. Jalankan Fase 1 collector "
        "dulu (tools/collect_signals.py) untuk menghasilkan korpus lokal.",
        allow_module_level=True,
    )


def _load_fixture_rows() -> list[dict]:
    rows = []
    with open(FIXTURE_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _row_by_message_id(rows: list[dict], message_id: int) -> dict:
    for row in rows:
        if row["message_id"] == message_id:
            return row
    raise KeyError(message_id)


def test_fixture_has_expected_real_messages():
    # Fixture bisa berupa 5 sample awal ATAU dump riwayat penuh (ribuan
    # pesan) — yang penting sample message_id yang dites spesifik di bawah
    # (3,4,5,6,7) memang ada di dalamnya.
    rows = _load_fixture_rows()
    assert len(rows) >= 5
    ids_present = {row["message_id"] for row in rows}
    assert {3, 4, 5, 6, 7}.issubset(ids_present)


def test_entry_signal_us30_single_entry_multi_tp():
    rows = _load_fixture_rows()
    row = _row_by_message_id(rows, 3)
    signal = parse_entry_signal(row["text"], message_id=3)

    assert signal is not None
    assert signal.symbol == "US30"
    assert signal.action == "SELL"
    assert signal.entry == 48500.0
    assert signal.entry_range is None
    assert signal.sl == 48560.0
    assert signal.tp == [48420.0, 48300.0, 48100.0]


def test_entry_signal_gold_entry_range():
    rows = _load_fixture_rows()
    row = _row_by_message_id(rows, 4)
    signal = parse_entry_signal(row["text"], message_id=4)

    assert signal is not None
    assert signal.symbol == "GOLD"
    assert signal.action == "SELL"
    assert signal.entry is None
    assert signal.entry_range == (4344.0, 4345.0)
    assert signal.sl == 4348.0
    assert signal.tp == [4333.0, 4323.0]


def test_entry_signal_usdjpy_decimal_prices():
    rows = _load_fixture_rows()
    row = _row_by_message_id(rows, 5)
    signal = parse_entry_signal(row["text"], message_id=5)

    assert signal is not None
    assert signal.symbol == "USDJPY"
    assert signal.action == "SELL"
    assert signal.entry == 156.600
    assert signal.sl == 156.80
    assert signal.tp == [156.00, 155.34, 154.45]


def test_live_update_messages_rejected_by_entry_parser():
    # pesan follow-up ("| Live Update") tidak boleh dianggap entry baru
    rows = _load_fixture_rows()
    for message_id in (6, 7):
        row = _row_by_message_id(rows, message_id)
        assert parse_entry_signal(row["text"], message_id=message_id) is None


def test_entry_signals_rejected_by_followup_parser():
    # sebaliknya: entry signal biasa bukan follow-up
    rows = _load_fixture_rows()
    for message_id in (3, 4, 5):
        row = _row_by_message_id(rows, message_id)
        assert parse_followup_regex(row["text"], message_id=message_id, reply_to_msg_id=None) is None


def test_followup_gold_ambiguous_choice_is_info_only():
    # "close fully position or Close partially and place your sl around 4349"
    # -> pilihan (or), SL bukan ke entry -> tidak ada aksi otomatis yang dipicu
    rows = _load_fixture_rows()
    row = _row_by_message_id(rows, 6)
    followup = parse_followup_regex(row["text"], message_id=6, reply_to_msg_id=None)

    assert followup is not None
    assert followup.symbol == "GOLD"
    assert followup.kinds == []


def test_followup_us30_clear_instructions_both_kinds_detected():
    # "You may close partially to secure gains and move the stop-loss to the entry."
    # -> instruksi tunggal (bukan pilihan "or"), dua aksi sekaligus terdeteksi
    rows = _load_fixture_rows()
    row = _row_by_message_id(rows, 7)
    followup = parse_followup_regex(row["text"], message_id=7, reply_to_msg_id=None)

    assert followup is not None
    assert followup.symbol == "US30"
    assert set(followup.kinds) == {"partial_close_tp1", "move_sl_be"}


def test_followup_move_sl_to_arbitrary_price_is_not_move_sl_be():
    # "place your sl around 4349" beda dari "move sl to entry" -> tidak boleh
    # ditebak sebagai move_sl_be
    followup = parse_followup_regex(
        "GOLD | Live Update\n\nplace your sl around 4349",
        message_id=999,
        reply_to_msg_id=None,
    )
    assert followup is not None
    assert "move_sl_be" not in followup.kinds


def test_entry_plain_sell_now_without_price_is_market_order():
    # ditemukan di dump riwayat: "Sell Now" tanpa angka entry sama sekali
    text = "US30 \n\nSell  Now\n\nTp.: 52260, 50100, 51940\nSl.: 52440\n\nRisk 1%"
    signal = parse_entry_signal(text, message_id=1001)
    assert signal is not None
    assert signal.action == "SELL"
    assert signal.entry is None
    assert signal.entry_range is None
    assert signal.sl == 52440.0
    assert signal.tp == [52260.0, 50100.0, 51940.0]


def test_entry_at_symbol_connector_with_now_and_level():
    # "Sell @ Now 52080" — '@' sebagai penghubung baru
    text = "US30 \n\nSell @ Now 52080\n\nTp.:  51950, 51850,\nsl.: 52105\n\nRisk 1%"
    signal = parse_entry_signal(text, message_id=1002)
    assert signal is not None
    assert signal.action == "SELL"
    assert signal.entry == 52080.0
    assert signal.tp == [51950.0, 51850.0]


def test_entry_at_symbol_connector_now_while_below_range():
    # "Sell @ Now While Below X" — kombinasi @ + now + while + below
    text = "GOLD  \n\nSell @ Now While Below 4060\n\nTp.:  4048, 4043, 4018\nsl.: 4063\n\nRisk 1%"
    signal = parse_entry_signal(text, message_id=1003)
    assert signal is not None
    assert signal.action == "SELL"
    assert signal.entry == 4060.0
    assert signal.entry_range is None


def test_entry_at_symbol_alone_no_now_no_below():
    # "Sell @ 7385" — cuma '@' langsung diikuti angka
    text = "SPX\n\nSell @ 7385\n\nTarget 7367 - 7342\nsl.: 7387\n\nrisk 1%"
    signal = parse_entry_signal(text, message_id=1004)
    assert signal is not None
    assert signal.action == "SELL"
    assert signal.entry == 7385.0
    assert signal.tp == [7367.0, 7342.0]
