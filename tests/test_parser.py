import sys

sys.path.insert(0, ".")

from src.parser.followup import parse_followup_regex  # noqa: E402
from src.parser.patterns import parse_entry_signal  # noqa: E402


# Sample teks asli ini SENGAJA di-embed literal (bukan lookup by message_id
# dari fixture) supaya test-nya tidak rapuh terhadap isi tests/fixtures/
# signals.jsonl yang bisa berubah (mis. dibersihkan dari data channel lama).
US30_ENTRY_TEXT = "US30\n\nSell now below 48500\n\nTarget 48420, 48300 , 48100\nSl.: 48560 \n\nRisk 1% ⚠️"
GOLD_RANGE_ENTRY_TEXT = "GOLD \n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348\n\nrisk 1%\nTimeframe 1h\nCurrent price 4344.8"
USDJPY_ENTRY_TEXT = "USDJPY \n\nSell Below 156.600\n\ntp.: 156.00, 155.34, 154.45\nsl.: 156.80\n\nrisk 1%\ntimeframe 1h, 4h\nCurrent price 156.59"
GOLD_AMBIGUOUS_FOLLOWUP_TEXT = (
    "GOLD | Live Update \n\nHit Profit about +100 pip✅\n\nand still has a bearish correctional "
    "toward 4323 while below 4346\n\na 15min above 4346 will be bullish momentum \nclose fully "
    "position or Close partially and place your sl around 4349\n\ntimeframe 1h, 15min\ncurrent price 4333"
)
US30_CLEAR_FOLLOWUP_TEXT = (
    "US30 | Live Update \n\nHit target +185 pip ✅\n\nIf the price breaks below 48,300 on a "
    "15-minute candle close, it is likely to decline toward 48,100.\n\nYou may close partially "
    "to secure gains and move the stop-loss to the entry.\n\nTimeframe 1h\ncurrent price  48315"
)


def test_entry_signal_us30_single_entry_multi_tp():
    signal = parse_entry_signal(US30_ENTRY_TEXT, message_id=3)

    assert signal is not None
    assert signal.symbol == "US30"
    assert signal.action == "SELL"
    assert signal.entry == 48500.0
    assert signal.entry_range is None
    assert signal.sl == 48560.0
    assert signal.tp == [48420.0, 48300.0, 48100.0]


def test_entry_signal_gold_entry_range():
    signal = parse_entry_signal(GOLD_RANGE_ENTRY_TEXT, message_id=4)

    assert signal is not None
    assert signal.symbol == "GOLD"
    assert signal.action == "SELL"
    assert signal.entry is None
    assert signal.entry_range == (4344.0, 4345.0)
    assert signal.sl == 4348.0
    assert signal.tp == [4333.0, 4323.0]


def test_entry_signal_usdjpy_decimal_prices():
    signal = parse_entry_signal(USDJPY_ENTRY_TEXT, message_id=5)

    assert signal is not None
    assert signal.symbol == "USDJPY"
    assert signal.action == "SELL"
    assert signal.entry == 156.600
    assert signal.sl == 156.80
    assert signal.tp == [156.00, 155.34, 154.45]


def test_live_update_messages_rejected_by_entry_parser():
    # pesan follow-up ("| Live Update") tidak boleh dianggap entry baru
    for text in (GOLD_AMBIGUOUS_FOLLOWUP_TEXT, US30_CLEAR_FOLLOWUP_TEXT):
        assert parse_entry_signal(text, message_id=6) is None


def test_entry_signals_rejected_by_followup_parser():
    # sebaliknya: entry signal biasa bukan follow-up
    for text in (US30_ENTRY_TEXT, GOLD_RANGE_ENTRY_TEXT, USDJPY_ENTRY_TEXT):
        assert parse_followup_regex(text, message_id=3, reply_to_msg_id=None) is None


def test_followup_gold_ambiguous_choice_is_info_only():
    # "close fully position or Close partially and place your sl around 4349"
    # -> pilihan (or), SL bukan ke entry -> tidak ada aksi otomatis yang dipicu
    followup = parse_followup_regex(GOLD_AMBIGUOUS_FOLLOWUP_TEXT, message_id=6, reply_to_msg_id=None)

    assert followup is not None
    assert followup.symbol == "GOLD"
    assert followup.kinds == []


def test_followup_us30_clear_instructions_both_kinds_detected():
    # "You may close partially to secure gains and move the stop-loss to the entry."
    # -> instruksi tunggal (bukan pilihan "or"), dua aksi sekaligus terdeteksi
    followup = parse_followup_regex(US30_CLEAR_FOLLOWUP_TEXT, message_id=7, reply_to_msg_id=None)

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


def test_entry_tp_label_typo_to_instead_of_tp():
    # "To.:" — typo channel untuk "Tp.:"
    text = "SPX500 \n\nSell @ now while below 7498\n\nTo.: 7481, 7461, 7442\nSl.: 7504\n\nRisk 1%"
    signal = parse_entry_signal(text, message_id=1005)
    assert signal is not None
    assert signal.tp == [7481.0, 7461.0, 7442.0]


def test_tp_label_to_does_not_false_match_total_or_today():
    # "Total Trades: 16" / "Today is a holiday" tidak boleh dianggap baris TP
    text = "GOLD\n\nSell Below 4033\n\nToday is a holiday\nTotal Trades: 16\nTp.: 4022, 4015\nsl.: 4036"
    signal = parse_entry_signal(text, message_id=1006)
    assert signal is not None
    assert signal.tp == [4022.0, 4015.0]


def test_entry_symbol_with_parenthetical_descriptor():
    # "US30 (Dow Jones)" — simbol dengan keterangan dalam kurung di baris yang sama
    text = "US30 (Dow Jones)\n\nSell @ Now \n\nTp.: 52220, 52120, 51950\nsl.: 52385\n\nRisk 1%"
    signal = parse_entry_signal(text, message_id=1007)
    assert signal is not None
    assert signal.symbol == "US30"
    assert signal.entry is None  # "Sell @ Now" tanpa angka -> market order


def test_entry_symbol_line_with_trailing_empty_pipe():
    # "GOLD | " — pipe nyasar tanpa isi, bukan header dekoratif sungguhan
    text = "GOLD | \n\nsell Below 4033\n\ntarget 4022, 4015\nsl.: 4036\n\nrisk 1%"
    signal = parse_entry_signal(text, message_id=1008)
    assert signal is not None
    assert signal.symbol == "GOLD"


def test_entry_symbol_line_with_real_decoration_still_rejected():
    # "GOLD | Bullish Setup" — dekorasi SUNGGUHAN, tetap ditolak regex
    # (diserahkan ke LLM fallback, bukan ditebak)
    text = "GOLD | Bullish Setup\n\nBuy Above 4342\nTarget 4355 - 4362\nstop loss 4340"
    signal = parse_entry_signal(text, message_id=1009)
    assert signal is None


def test_followup_header_with_arbitrary_word_before_update():
    # "Final Update" / "Trade Update" — bukan cuma "Live"/"New"
    for header in ["GOLD | Final Update", "USNAS100 | Trade Update"]:
        followup = parse_followup_regex(f"{header}\n\nSome commentary here", message_id=1010, reply_to_msg_id=None)
        assert followup is not None, f"gagal untuk header: {header}"


def test_sl_label_stop_loss_two_words():
    text = "SPX\n\nsell below 6548\n\nstop loss 6551\n\ntarget 6535 - 6500 - 6460"
    signal = parse_entry_signal(text, message_id=1011)
    assert signal is not None
    assert signal.sl == 6551.0


def test_direction_connector_from_and_again():
    text1 = "GOLD\n\nSell from 5077\n\nTp.: 5067, 5060, 5027\nSl.: 5081"
    signal1 = parse_entry_signal(text1, message_id=1012)
    assert signal1 is not None
    assert signal1.entry == 5077.0

    text2 = "GOLD\n\nSell AGAIN below 5024\n\nTarget: 5007, 4983, 4966\nSl: 5028"
    signal2 = parse_entry_signal(text2, message_id=1013)
    assert signal2 is not None
    assert signal2.entry == 5024.0


def test_symbol_and_direction_crammed_same_line_no_separator():
    # "spx sell below 6548" — simbol dan arah nyatu di satu baris tanpa '|'
    text = "spx sell below 6548\n\nstop loss 6551\n\ntarget 6535 - 6500 - 6460"
    signal = parse_entry_signal(text, message_id=1014)
    assert signal is not None
    assert signal.symbol == "SPX"
    assert signal.entry == 6548.0


def test_symbol_and_direction_crammed_with_pipe():
    # "GOLD | Sell Now below 4542" — simbol+arah di baris sama dgn '|', BUKAN dekorasi
    text = "GOLD | Sell Now below 4542\n\nsl 4544\n\nTarget 4500, 4480"
    signal = parse_entry_signal(text, message_id=1015)
    assert signal is not None
    assert signal.symbol == "GOLD"
    assert signal.entry == 4542.0


def test_tp_typo_with_wrong_direction_is_filtered_out_not_kept():
    # kasus nyata #4649: "Sell from 5077 ... Tp.: 50670, 5060, 5027" -- 50670
    # jelas typo (kelebihan digit dari 5067), posisinya JAUH DI ATAS entry
    # padahal utk SELL semua TP harus di BAWAH entry. Harus difilter, bukan dipakai.
    text = "GOLD\n\nSell from 5077\n\nTp.: 50670, 5060, 5027\nSl.: 5081"
    signal = parse_entry_signal(text, message_id=1016)
    assert signal is not None
    assert signal.tp == [5060.0, 5027.0]
    assert 50670.0 not in signal.tp


def test_all_tp_wrong_direction_rejects_whole_signal():
    text = "GOLD\n\nBuy above 4342\n\nTarget 4300, 4310\nsl.: 4338"
    # semua TP (4300, 4310) di BAWAH entry (4342), padahal BUY butuh TP di ATAS -> tolak semua
    signal = parse_entry_signal(text, message_id=1017)
    assert signal is None


def test_sl_wrong_direction_rejects_whole_signal():
    # SELL tapi SL di BAWAH entry (harusnya di ATAS) -> data tidak masuk akal, tolak
    text = "GOLD\n\nSell below 4344\n\nTarget 4330, 4320\nsl.: 4340"
    signal = parse_entry_signal(text, message_id=1018)
    assert signal is None


def test_buy_valid_direction_all_pass():
    text = "GOLD\n\nBuy above 4342\n\nTarget 4355, 4362, 4373\nsl.: 4338"
    signal = parse_entry_signal(text, message_id=1019)
    assert signal is not None
    assert signal.tp == [4355.0, 4362.0, 4373.0]
    assert signal.sl == 4338.0
