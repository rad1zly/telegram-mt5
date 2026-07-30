"""Klasifikasi pesan follow-up ("Live Update") yang merujuk ke posisi
yang sudah terbuka. Tidak ada reply_to_msg_id di data nyata yang diamati
(channel post biasa, bukan reply) — jadi follow-up dicocokkan ke posisi
terbuka lewat SYMBOL, bukan threading.

PRINSIP (penting, ini menyangkut uang sungguhan): channel ini menulis
follow-up dengan bahasa saran/kondisional ("you may...", "we recommend...",
"...or..."), bukan selalu perintah tegas satu-arti. Keputusan produk: alih-alih
diam saat bahasa ambigu, rekomendasi channel TETAP DIEKSEKUSI dengan default
yang masuk akal -- karena user memantau performa channel ini dan mau
rekomendasinya benar-benar dijalankan, bukan cuma diteruskan sebagai notifikasi
pasif.

- move_sl_be dipicu kalau teks bilang SL dipindah/ditaruh ("move"/"place") ke
  ENTRY/BREAKEVEN secara eksplisit. "place your sl around 4349" (harga baru
  yang BUKAN entry) tidak match -> tidak memicu move_sl_be (tapi mekanisme
  SL+ otomatis di executor tetap jalan begitu partial_close_tp1 sukses,
  independen dari ini).
- close_all JUGA dipicu oleh narasi bentuk LAMPAU "Closed the position/trade"
  -- channel menceritakan mereka SUDAH cut-loss/tutup posisi sendiri; ini
  mengikuti keputusan real-time mereka (tutup juga posisi kita), bukan
  membiarkannya menunggu SL asli yang lebih jauh.
- "Hit Target"/"Hit Profit"/dst adalah HEADLINE PENANDA PROGRES, BUKAN
  instruksi -- diuji lewat backtest: kalau headline ini dipaksa selalu jadi
  close_all (mengalahkan instruksi partial di badan pesan), rata-rata profit
  per aksi turun sampai nyaris $0 (channel sering punya BEBERAPA target
  berurutan -- "Hit Target" pertama baru separuh jalan, bukan akhir trade).
  Jadi headline INI SENDIRI TIDAK menentukan aksi; instruksi eksplisit di
  badan pesan (partial vs fully/all/positions) yang menentukan.
- partial_close_tp1 dipicu oleh instruksi "close partial(ly)" yang jelas,
  ATAU oleh bahasa "secure/protect profit(s)" tanpa kata "close" eksplisit
  (mis. "we recommend securing your profits") -- di korpus, frasa ini hampir
  selalu jadi alasan/pelengkap instruksi partial ("close partially TO SECURE
  your profits"), bukan perintah tutup penuh berdiri sendiri.
- close_all dipicu HANYA oleh instruksi tutup PENUH yang eksplisit ("close
  the position/all/fully/positions", tanpa kata "partial").
- Kalimat PILIHAN eksplisit ("close fully OR partially") DIRESOLVE ke
  partial_close_tp1 (opsi lebih konservatif), bukan ditekan/diam.
- Kalau benar-benar tidak ada kategori yang cocok sama sekali (murni info,
  mis. "+50 pip, masih jalan"), kinds=[] -- diteruskan sebagai notifikasi.
"""

import re
from typing import Optional

from src.parser.schema import FollowUp

LIVE_UPDATE_HEADER_RE = re.compile(
    r"^\s*([A-Za-z0-9]+)"                    # simbol pertama
    r"(?:\s*&\s*[A-Za-z0-9]+)?"              # simbol kedua opsional ("SYM1 & SYM2 | ..."), diabaikan untuk sekarang
    r"\s*[-|]?\s*"                            # separator: '|', '-', atau cuma spasi ("GOLD LIVE UPDATE")
    r"(?:\w+\s+)?"                             # kata sifat opsional sebelum "update" — bebas
                                                # (diamati: "Live", "New", "Final", "Trade", dll)
    r"update\b",
    re.IGNORECASE,
)

MOVE_SL_BE_RE = re.compile(
    r"\b(?:mov(?:e|ing)|plac(?:e|ing))\b[^.\n]{0,40}\b(?:stop.loss|stop\s+loss|sl)\b[^.\n]{0,20}\b(?:to|at)\b[^.\n]{0,15}"
    r"\b(?:the\s+)?(?:entry|breakeven|be)\b",
    re.IGNORECASE,
)
# Kata sisipan antara "close" dan target-nya, mis. "close THE POSITION
# partially", "close IT fully", "close THIS TRADE" — ditemukan sangat umum
# di korpus nyata, regex versi awal (cuma "close partial(ly)" persis
# nempel) melewatkan puluhan instruksi close yang jelas.
_CLOSE_FILLER = r"(?:the\s+position|this\s+trade|this\s+position|the\s+trade|it)\s+"

# "clos(e|ing)" (bukan cuma "close") -- korpus asli sering pakai bentuk
# gerund ("we recommend CLOSING part of the position..."), dan urutan kata
# terbalik ("PARTIALLY close", bukan "close partially") -- kedua pola ini
# awalnya lolos tak terklasifikasi (kinds=[]) walau jelas instruksi partial
# close. Lihat juga CLOSE_ALL_RE utk simbol yg nyempil ("closing the
# USNAS100 position").
PARTIAL_CLOSE_RE = re.compile(
    rf"\bclos(?:e|ing)\b[^.\n]{{0,40}}\bpartial(?:ly)?\b"
    rf"|\bpartial(?:ly)?\b[^.\n]{{0,40}}\bclos(?:e|ing)\b"
    # "close/closing PART OF the position/it" -- kata "part" (bukan
    # "partial(ly)") juga sangat umum dipakai utk maksud yang sama persis.
    rf"|\bclos(?:e|ing)\b[^.\n]{{0,15}}\bpart\s+of\b",
    re.IGNORECASE,
)

# "secure/protect profit(s)" TANPA kata close/partial/full eksplisit -- masih
# instruksi nyata dari channel (present tense: "we recommend securing your
# profits"), bukan cuma narasi. Bentuk lampau "secured" dikecualikan via
# \b(?:secur(?:e|ing)|protect(?:ing)?)\b (BUKAN "secured") supaya tidak salah
# memicu dari kalimat yang cuma menceritakan kejadian lalu ("we secured
# partial profits earlier..."). Diperlakukan sebagai partial_close_tp1 (lihat
# docstring modul) -- SENGAJA dipisah dari PARTIAL_CLOSE_RE supaya tidak ikut
# memicu deteksi ambigu kalau muncul bersama close_all yang sudah jelas
# ("Close the position fully and secure your profits." -> tetap close_all).
SECURE_PROFIT_RE = re.compile(
    r"\b(?:secur(?:e|ing)|protect(?:ing)?)\b[^.\n]{0,40}\b(?:profit|gain)s?\b",
    re.IGNORECASE,
)

CLOSE_ALL_RE = re.compile(
    rf"\bclos(?:e|ing)\s+(?:{_CLOSE_FILLER})?(?:all|fully|full\s+position)\b"
    # "close the position/this trade/it" TANPA embel-embel apa pun juga
    # berarti tutup penuh (kalau maksudnya sebagian, penulis akan bilang
    # "partially") — asal TIDAK diikuti "partial" (dicegah lookahead).
    rf"|\bclos(?:e|ing)\s+(?:the\s+position|this\s+trade|this\s+position|the\s+trade|it)\b(?!\s+partial)"
    # "closing the USNAS100 position" / "closing the Gold position" -- nama
    # simbol (1-2 kata) sering nyempil antara "the" dan "position" di
    # korpus asli. Lookahead partial tetap dijaga di akhir.
    rf"|\bclos(?:e|ing)\s+the\s+(?:\w+\s+){{1,2}}position\b(?!\s+partial)"
    # "close positions" polos (jamak, tanpa artikel), ATAU "close the
    # positions" (jamak DENGAN artikel, mis. "Close the positions on
    # USNAS100 and US30") -- keduanya ditemukan di korpus.
    rf"|\bclos(?:e|ing)\s+(?:the\s+)?positions?\b(?!\s+partial)"
    # "Closed the position/trade" bentuk LAMPAU -- channel menceritakan
    # mereka SUDAH menutup posisi mereka sendiri (biasanya cut-loss manual).
    # Ini instruksi implisit: posisi KITA juga harus ditutup, mengikuti
    # keputusan mereka, bukan menunggu SL asli yang lebih jauh. SENGAJA
    # dibatasi ke "the position"/"the trade"/"it" persis (bukan pola
    # fleksibel simbol-nyempil di atas) supaya tidak salah tangkap "closed"
    # dalam konteks candle (mis. "closed a 15min candle below X").
    rf"|\bclosed\s+(?:the\s+position|the\s+trade|it)\b(?!\s+partial)",
    re.IGNORECASE,
)

# "close fully position OR close partially", "close positions fully or
# partially", "closing... or closing..." — pilihan eksplisit dari channel
# sendiri. DIRESOLVE ke partial_close_tp1 (opsi lebih konservatif), BUKAN
# ditekan -- lihat parse_followup_regex.
AMBIGUOUS_CLOSE_CHOICE_RE = re.compile(
    r"\bclos(?:e|ing)\b[^.\n]{0,60}\bor\b[^.\n]{0,60}\bclos(?:e|ing)\b"
    # "close(ing) the position FULLY or PARTIALLY" -- kata di antara "fully"
    # dan "or" bervariasi ("fully", "fully position", "fully.") jadi dicek
    # via jarak karakter, bukan frasa persis.
    r"|\bfull(?:y)?\b[^.\n]{0,25}\bor\b[^.\n]{0,25}\bpartial(?:ly)?\b"
    r"|\bpartial(?:ly)?\b[^.\n]{0,25}\bor\b[^.\n]{0,25}\bfull(?:y)?\b",
    re.IGNORECASE,
)


def extract_symbol_from_live_update(text: str) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    match = LIVE_UPDATE_HEADER_RE.match(first_line)
    return match.group(1).upper() if match else None


def classify_followup_kinds(text: str) -> list[str]:
    """Logika inti deteksi aksi (move_sl_be/partial_close_tp1/close_all) dari
    ISI teks saja -- TIDAK bergantung header 'Live Update' sama sekali.
    Dipisah dari parse_followup_regex supaya bisa dipakai langsung saat
    reply_to_msg_id sudah memastikan pesan ini follow-up ke trade tertentu
    (banyak pesan follow-up nyata TIDAK punya header/simbol di teksnya
    sendiri karena disebut di pesan sebelumnya dalam thread yang sama --
    lihat backtest/runner.py:_resolve_trade_via_reply_chain)."""
    kinds: list[str] = []
    if MOVE_SL_BE_RE.search(text):
        kinds.append("move_sl_be")

    partial_matched = bool(PARTIAL_CLOSE_RE.search(text))
    close_all_matched = bool(CLOSE_ALL_RE.search(text))
    # Kalau KEDUANYA match sekaligus (atau frasa pilihan eksplisit terdeteksi),
    # teks itu menyebut DUA opsi close berbeda dalam satu kalimat ("fully or
    # partially", dst) -- channel sendiri tidak berkomitmen ke satu pilihan.
    # Default produk: pilih partial_close_tp1 (opsi lebih konservatif -- tetap
    # mengunci sebagian profit + memicu SL+ otomatis, tanpa menutup penuh
    # posisi yang mungkin masih berjalan), BUKAN diam. Catatan: SECURE_PROFIT_RE
    # SENGAJA tidak ikut dicek di sini -- dia cuma alasan/justifikasi yang
    # sering menempel di kalimat close_all yang sudah jelas ("close fully AND
    # secure your profits"), bukan opsi kedua yang bersaing.
    ambiguous = (partial_matched and close_all_matched) or bool(AMBIGUOUS_CLOSE_CHOICE_RE.search(text))

    if ambiguous:
        kinds.append("partial_close_tp1")
    elif close_all_matched:
        kinds.append("close_all")
    elif partial_matched or SECURE_PROFIT_RE.search(text):
        kinds.append("partial_close_tp1")

    return kinds


def parse_followup_regex(
    text: str,
    message_id: int,
    reply_to_msg_id: Optional[int],
) -> Optional[FollowUp]:
    """Return None kalau ini bukan pesan follow-up berformat 'Live Update'
    sama sekali (biar dilempar ke pipeline lain / diabaikan). Return
    FollowUp dengan kinds=[] kalau ini follow-up tapi tidak ada aksi
    otomatis yang cocok — BEDA dari None (bukan follow-up sama sekali).

    CATATAN: ini mensyaratkan header 'Live Update' + simbol di baris
    pertama. Untuk pesan TANPA header itu (simbol disebut di pesan
    sebelumnya via reply), pemanggil (mis. backtest/runner.py) yang sudah
    berhasil resolve trade lewat reply_to_msg_id sebaiknya panggil
    classify_followup_kinds() langsung, bukan fungsi ini."""
    symbol = extract_symbol_from_live_update(text)
    if symbol is None:
        return None

    kinds = classify_followup_kinds(text)

    return FollowUp(
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
        kinds=kinds,
        raw_text=text,
        symbol=symbol,
    )
