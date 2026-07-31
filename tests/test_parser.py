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


def test_followup_gold_ambiguous_choice_resolves_to_partial_close():
    # "close fully position or Close partially and place your sl around 4349"
    # -> pilihan (or) diresolve ke partial_close_tp1 (opsi lebih konservatif).
    # "Hit Profit" di headline TIDAK memaksa close_all -- terbukti lewat
    # backtest kalau dipaksa, rata-rata profit per aksi turun ke nyaris $0
    # (channel sering punya beberapa target berurutan, "Hit Profit" pertama
    # baru separuh jalan).
    followup = parse_followup_regex(GOLD_AMBIGUOUS_FOLLOWUP_TEXT, message_id=6, reply_to_msg_id=None)

    assert followup is not None
    assert followup.symbol == "GOLD"
    assert followup.kinds == ["partial_close_tp1"]


def test_followup_us30_clear_instructions_both_kinds_detected():
    # "Hit target +185 pip ... close partially to secure gains and move the
    # stop-loss to the entry." -> instruksi partial + BE tetap dieksekusi;
    # headline "Hit target" tidak override ke close_all (lihat catatan test
    # sebelumnya).
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


def test_followup_close_position_fully_with_filler_words():
    # "Close the position fully" -- kata sisipan "the position" antara
    # "close" dan "fully" (bukan "close fully" langsung nempel)
    text = "SPX500 | Update\n\nFinal Target Hit +480 Pip\n\nClose the position fully and secure your profits."
    followup = parse_followup_regex(text, message_id=1020, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_close_the_position_bare_means_close_all():
    # "close the position" TANPA embel-embel apa pun -> berarti tutup penuh
    text = "US30 | Live Update\n\nHit Profit +110 pip\n\nWe now prefer to close the position due to the current geopolitical situation."
    followup = parse_followup_regex(text, message_id=1021, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_close_the_position_partially_with_filler_words():
    # "close the position partially" -- kata sisipan sebelum "partially".
    # SENGAJA tanpa headline "Hit Target"/"secure profits" supaya menguji
    # deteksi partial murni, terisolasi dari prioritas close_all yang lebih
    # tinggi (lihat test_followup_us30_hit_target_overrides_partial_to_close_all
    # utk kasus headline hit-target yang override ini).
    text = "GOLD | Live Update\n\nStill running +70 pip.\n\nYou may close the position partially and move your stop loss to around 4061."
    followup = parse_followup_regex(text, message_id=1022, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["partial_close_tp1"]


def test_followup_ambiguous_close_with_filler_words_resolves_to_partial():
    # "close the position fully, or close it partially" -- filler + pilihan "or"
    # -> diresolve ke partial_close_tp1 (bukan ke-detect keduanya, bukan ditekan).
    # SENGAJA tanpa headline "Hit Target"/"secure profits" -- lihat catatan di
    # test_followup_close_the_position_partially_with_filler_words.
    text = (
        "GOLD | Live Update\n\nDue to the high market volatility, "
        "you may close the position fully, or close it partially and move your stop loss "
        "to around 4092."
    )
    followup = parse_followup_regex(text, message_id=1023, reply_to_msg_id=None)
    assert followup is not None
    assert "partial_close_tp1" in followup.kinds
    assert "close_all" not in followup.kinds


def test_followup_gerund_closing_and_moving_both_detected():
    # Bentuk gerund ("we recommend CLOSING...and MOVING...") -- ditemukan
    # sangat umum di korpus asli (99+ pesan), regex versi awal cuma cocok
    # bentuk dasar "close"/"move" dan melewatkan semua ini. SENGAJA tanpa
    # "secure your profits" di akhir (itu sekarang prioritas close_all --
    # lihat test_followup_secure_profits_without_close_word_triggers_close_all).
    text = (
        "US30 | Live Update\n\nThe price has reached the 52300 level, still moving.\n\n"
        "For now, we recommend closing part of the position and moving "
        "your stop loss to breakeven or slightly above your entry."
    )
    followup = parse_followup_regex(text, message_id=1024, reply_to_msg_id=None)
    assert followup is not None
    assert set(followup.kinds) == {"partial_close_tp1", "move_sl_be"}


def test_followup_reversed_word_order_partially_close():
    # Urutan kata terbalik: "PARTIALLY Close" (bukan "close partially").
    # SENGAJA tanpa headline "Hit Target"/"secure profits" -- lihat catatan
    # di test_followup_close_the_position_partially_with_filler_words.
    text = "GOLD | Live Update\n\nStill running.\n\nPartially Close and move your stop loss to around 4036."
    followup = parse_followup_regex(text, message_id=1025, reply_to_msg_id=None)
    assert followup is not None
    assert "partial_close_tp1" in followup.kinds


def test_followup_close_all_with_symbol_name_between_the_and_position():
    # Nama simbol nyempil di antara "the" dan "position": "closing the
    # USNAS100 position" -- pola lama cuma cocok "the position" persis nempel.
    text = "USNAS100 | Live Update\n\nHit Target +150 Pip\n\nWe recommend closing the USNAS100 position due to the shift in momentum."
    followup = parse_followup_regex(text, message_id=1026, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_close_positions_bare_plural():
    text = "GOLD | Live Update\n\nHit Target +90 Pip\n\nYou may close positions now, and wait for a confirmed reversal before re-entering."
    followup = parse_followup_regex(text, message_id=1027, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_secure_profits_without_close_word_triggers_partial_close():
    # "we recommend securing your profits" -- TANPA kata "close" sama sekali,
    # tapi tetap instruksi nyata (present tense) -- diresolve ke partial close
    # (di korpus, frasa ini hampir selalu jadi alasan pelengkap instruksi
    # partial, bukan perintah tutup penuh berdiri sendiri).
    text = "GOLD | Live Update\n\nDue to elevated volatility, we recommend securing your profits."
    followup = parse_followup_regex(text, message_id=1029, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["partial_close_tp1"]


def test_followup_secured_past_tense_narration_is_not_a_new_instruction():
    # "we secured partial profits earlier" -- bentuk LAMPAU, cuma menceritakan
    # kejadian yang sudah terjadi, BUKAN instruksi baru -> tidak memicu apa pun.
    text = (
        "GOLD | Live Update\n\nThe price failed to sustain below 21,380 and reversed, "
        "triggering the adjusted stop loss at 21,400. However, we secured partial "
        "profits earlier, reducing overall risk exposure."
    )
    followup = parse_followup_regex(text, message_id=1030, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == []


def test_followup_close_fully_with_secure_profit_justification_stays_close_all():
    # "Close the position fully and secure your profits." -- close_all yang
    # JELAS, dengan "secure your profits" cuma sebagai alasan, BUKAN pilihan
    # kedua yang bersaing. Tidak boleh salah ke-flag ambigu jadi partial.
    text = "SPX500 | Update\n\nFinal Target Hit +480 Pip\n\nClose the position fully and secure your profits."
    followup = parse_followup_regex(text, message_id=1031, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_fully_or_partially_conflict_resolves_to_partial_even_with_gerund():
    # "closing the position fully or partially" -- versi gerund dari kasus
    # ambigu; sebelumnya regex versi awal malah diam-diam menganggap ini
    # "close_all" (menebak salah satu opsi tanpa tanda) karena literal
    # "close ... fully" match tapi "or partially"-nya tidak terdeteksi sebagai
    # pilihan. Sekarang benar-benar diresolve ke partial_close_tp1 (opsi
    # konservatif, sesuai keputusan produk), bukan close_all yang tak sengaja.
    # SENGAJA tanpa headline "Hit Target" -- itu prioritas lebih tinggi yang
    # akan override logika ambigu ini (lihat test khusus utk itu).
    text = (
        "SPX500 | Live Update\n\nDue to high volatility, we recommend "
        "closing the position fully or partially and moving your stop loss to breakeven."
    )
    followup = parse_followup_regex(text, message_id=1028, reply_to_msg_id=None)
    assert followup is not None
    assert "close_all" not in followup.kinds
    assert "partial_close_tp1" in followup.kinds
    assert "move_sl_be" in followup.kinds


def test_followup_place_sl_at_entry_triggers_move_sl_be():
    # "place your sl at entry" -- verba "place" (bukan "move"), ditemukan
    # lewat pembacaan manual korpus (msg 3421 asli).
    text = "USNAS100 - Live Update\n\nHit Target +80 pip\n\nplace your sl at entry or around 24470"
    followup = parse_followup_regex(text, message_id=1032, reply_to_msg_id=None)
    assert followup is not None
    assert "move_sl_be" in followup.kinds


def test_followup_place_sl_at_arbitrary_price_is_not_move_sl_be():
    # "place your sl around 3758" -- harga baru yang BUKAN entry -> tetap
    # tidak memicu apa pun (tidak bisa aman auto-set harga SL sembarang).
    text = "GOLD - Live Update\n\nHit Target +120 pip\n\nplace your sl around 3758"
    followup = parse_followup_regex(text, message_id=1033, reply_to_msg_id=None)
    assert followup is not None
    # Inti test ini: harga SL sembarang (bukan entry) TIDAK boleh memicu
    # move_sl_be -- kita tidak bisa aman menaruh SL di angka acak.
    assert "move_sl_be" not in followup.kinds
    assert "partial_close_tp1" not in followup.kinds
    assert "close_all" not in followup.kinds
    # "Hit Target +120 pip" sendiri sekarang dikenali sbg pengumuman hasil
    # (target_reached) -- aksinya diputuskan config, lihat followup.py.
    assert followup.kinds == ["target_reached"]


def test_followup_closed_the_position_past_tense_triggers_close_all():
    # "Closed the position with a loss" -- bentuk LAMPAU, channel bilang
    # mereka SUDAH cut-loss/tutup posisi sendiri (msg 2987 asli) -> ikuti
    # keputusan real-time mereka, tutup juga posisi kita.
    text = "GOLD - UPDATE\n\nClosed the position with a loss of around -25 pip, as price closed a 5-minute candle above 3332."
    followup = parse_followup_regex(text, message_id=1034, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]


def test_followup_closed_candle_context_is_not_close_all():
    # "closed a 1H candle above X" -- "closed" di sini soal candle, BUKAN
    # posisi -> tidak boleh salah kena pola close_all bentuk lampau.
    text = "GOLD | Update\n\nThe price broke above 3381 and closed a 1H candle above it, followed by continuation toward 3401."
    followup = parse_followup_regex(text, message_id=1035, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == []


def test_followup_close_the_positions_plural_with_article():
    # "Close the positions on USNAS100 and US30" -- jamak DENGAN artikel
    # "the" (msg 4326 asli) -- pola lama cuma cocok jamak TANPA artikel.
    text = "USNAS100 | Live Update\n\nClose the positions on USNAS100 and US30, as the GDP data was released stronger than expected."
    followup = parse_followup_regex(text, message_id=1036, reply_to_msg_id=None)
    assert followup is not None
    assert followup.kinds == ["close_all"]
