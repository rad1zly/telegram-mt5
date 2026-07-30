"""Klasifikasi pesan follow-up ("Live Update") yang merujuk ke posisi
yang sudah terbuka. Tidak ada reply_to_msg_id di data nyata yang diamati
(channel post biasa, bukan reply) — jadi follow-up dicocokkan ke posisi
terbuka lewat SYMBOL, bukan threading.

PRINSIP KONSERVATIF (penting, ini menyangkut uang sungguhan): channel ini
menulis follow-up dengan bahasa saran/kondisional ("you may...", "...or
..."), bukan perintah tegas. Regex di sini SENGAJA ketat:

- move_sl_be HANYA dipicu kalau teks bilang SL dipindah ke ENTRY/BREAKEVEN
  secara eksplisit. "place your sl around 4349" (harga baru yang BUKAN
  entry) tidak match -> jatuh ke kinds kosong (info-only), bukan ditebak.
- partial_close_tp1 dipicu oleh instruksi "close partial(ly)" yang jelas.
  Kalimat pilihan seperti "close fully OR close partially" tetap match
  literal "close partially"-nya; sengaja begitu karena keduanya opsi yang
  disebut penulis signal sendiri — tapi lihat catatan di parse_followup_regex
  soal kapan sebaiknya ini diserahkan ke LLM fallback saja.
- Kalau tidak ada kategori yang cocok, kinds=[] (info-only, cuma
  diteruskan sebagai notifikasi ke user, tidak dieksekusi).
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
    r"\bmove\b[^.\n]{0,40}\b(?:stop.loss|stop\s+loss|sl)\b[^.\n]{0,20}\bto\b[^.\n]{0,15}"
    r"\b(?:the\s+)?(?:entry|breakeven|be)\b",
    re.IGNORECASE,
)
# Kata sisipan antara "close" dan target-nya, mis. "close THE POSITION
# partially", "close IT fully", "close THIS TRADE" — ditemukan sangat umum
# di korpus nyata, regex versi awal (cuma "close partial(ly)" persis
# nempel) melewatkan puluhan instruksi close yang jelas.
_CLOSE_FILLER = r"(?:the\s+position|this\s+trade|this\s+position|the\s+trade|it)\s+"

PARTIAL_CLOSE_RE = re.compile(rf"\bclose\s+(?:{_CLOSE_FILLER})?partial(?:ly)?\b", re.IGNORECASE)

CLOSE_ALL_RE = re.compile(
    rf"\bclose\s+(?:{_CLOSE_FILLER})?(?:all|fully|full\s+position)\b"
    # "close the position/this trade/it" TANPA embel-embel apa pun juga
    # berarti tutup penuh (kalau maksudnya sebagian, penulis akan bilang
    # "partially") — asal TIDAK diikuti "partial" (dicegah lookahead).
    rf"|\bclose\s+(?:the\s+position|this\s+trade|this\s+position|the\s+trade|it)\b(?!\s+partial)",
    re.IGNORECASE,
)

# "close fully position OR close partially" — pilihan, bukan instruksi tegas.
# Kalau pola ini match, TIDAK ADA aksi close yang dipicu otomatis dari
# kalimat itu (partial_close_tp1 ikut ditekan juga, karena dia yang benar-benar
# dieksekusi otomatis — salah pilih di sini berarti uang sungguhan bergerak).
AMBIGUOUS_CLOSE_CHOICE_RE = re.compile(r"\bclose\b[^.\n]{0,60}\bor\b[^.\n]{0,60}\bclose\b", re.IGNORECASE)


def extract_symbol_from_live_update(text: str) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    match = LIVE_UPDATE_HEADER_RE.match(first_line)
    return match.group(1).upper() if match else None


def parse_followup_regex(
    text: str,
    message_id: int,
    reply_to_msg_id: Optional[int],
) -> Optional[FollowUp]:
    """Return None kalau ini bukan pesan follow-up berformat 'Live Update'
    sama sekali (biar dilempar ke pipeline lain / diabaikan). Return
    FollowUp dengan kinds=[] kalau ini follow-up tapi tidak ada aksi
    otomatis yang cocok — BEDA dari None (bukan follow-up sama sekali)."""
    symbol = extract_symbol_from_live_update(text)
    if symbol is None:
        return None

    kinds: list[str] = []
    if MOVE_SL_BE_RE.search(text):
        kinds.append("move_sl_be")

    if not AMBIGUOUS_CLOSE_CHOICE_RE.search(text):
        if PARTIAL_CLOSE_RE.search(text):
            kinds.append("partial_close_tp1")
        if CLOSE_ALL_RE.search(text):
            kinds.append("close_all")

    return FollowUp(
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
        kinds=kinds,
        raw_text=text,
        symbol=symbol,
    )
