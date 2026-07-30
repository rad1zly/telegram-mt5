"""Parser regex untuk pesan entry signal, dibangun dari korpus nyata di
tests/fixtures/signals.jsonl. Format channel yang diamati:

    <SYMBOL>

    <Buy/Sell> [now] [below/above] <entry atau rentang entry>

    <Target|tp>[.,:]* <tp1>, <tp2>, ...
    <Sl>[.,:]* <angka>

    [risk ...] [timeframe ...] [current price ...]  <- diabaikan

Channel ini tidak konsisten soal tanda baca setelah label ("Sl.", "Sl:",
"Sl,", bahkan "Sl,.") — jadi label diikuti sembarang gabungan titik/koma/
titik-dua sebelum spasi+angka, BUKAN cuma titik-opsional seperti draf awal.

Prinsip sama dengan modul lain: kalau simbol, arah, SL, atau TP tidak
ketemu ATAU gagal dikonversi ke angka, return None — jangan mengarang
nilai, dan jangan pernah biarkan satu pesan aneh melempar exception yang
menghentikan seluruh batch. Pesan "Live Update" / follow-up sengaja
ditolak di sini (lihat followup.py), supaya tidak salah dianggap entry
baru.
"""

import re
from typing import Optional

from src.parser.schema import Signal

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{2,9}$", re.IGNORECASE)

# Wajib diawali digit asli (bukan koma) supaya label yang diikuti koma
# nyasar (mis. "Sl, 23260") tidak ikut tertangkap sebagai bagian angka.
_NUM = r"\d[\d,]*\.?\d*"

DIRECTION_RE = re.compile(
    # "@"/"now"/"while"/"from"/"again" bisa muncul dalam kombinasi/urutan
    # apapun sebelum below/above (diamati: "Sell now below X",
    # "Sell While Above X", "Sell Now while below X", "Sell @ Now 52080",
    # "Sell @ 7385", "Sell From 5077", "Sell AGAIN below X").
    r"\b(buy|sell)\b\s*(?:@\s*)?(?:(?:now|while|from|again)\s+)*(?:below|above)?\s*"
    rf"({_NUM})\s*(?:-\s*({_NUM}))?",
    re.IGNORECASE,
)

# Fallback kalau DIRECTION_RE gagal (tidak ada angka entry sama sekali) —
# "Sell Now" / "Buy Now" polos berarti market order tanpa level spesifik.
# Wajib ada kata "now" sebagai penanda keyakinan; "Sell"/"Buy" saja terlalu
# ambigu (bisa muncul di kalimat commentary biasa).
MARKET_DIRECTION_RE = re.compile(r"\b(buy|sell)\b\s*(?:@\s*)?now\b", re.IGNORECASE)

# \b setelah alternasi wajib — tanpa itu "to" bisa salah cocok dengan awal
# kata "Total"/"Today"/"Touched" dll.
TP_LINE_RE = re.compile(r"^\s*(?:target|tp|to)\b[.,:]*\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# "sl" ATAU "stop loss" (dua kata, spasi bebas) sebagai label.
SL_LINE_RE = re.compile(rf"^\s*(?:sl|stop\s*loss)\b[.,:]*\s*({_NUM})", re.IGNORECASE | re.MULTILINE)


def _to_float(raw: str) -> Optional[float]:
    """None kalau gagal dikonversi — dipakai sebagai sinyal 'tolak', bukan
    exception yang bisa menghentikan seluruh batch parsing."""
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _parse_number_list(raw: str) -> list[float]:
    # Channel kadang pisah TP pakai koma ("48420, 48300"), kadang pakai
    # dash ("Target 7367 - 7342") — dua-duanya diterima. Harga di
    # instrumen ini selalu positif jadi "-" aman dianggap separator,
    # bukan tanda minus.
    values = []
    for part in re.split(r"[,\-]", raw):
        part = part.strip()
        if not part:
            continue
        value = _to_float(part)
        if value is not None:
            values.append(value)
    return values


def parse_entry_signal(text: str, message_id: int) -> Optional[Signal]:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None

    first_line = lines[0]
    # Pesan follow-up ("Live Update"/"Update") ditolak di sini (ditangani
    # followup.py), independen dari struktur "|" di bawah.
    if "update" in first_line.lower():
        return None

    # "|" di header BELUM TENTU berarti dekorasi — kadang cuma nyasar tanpa
    # isi ("GOLD | "), dan kadang malah berisi arah+harga yang di-cram di
    # baris yang sama ("GOLD | Sell Now below 4542"). Simbol diterima
    # selama teks setelah "|" (kalau ada) KOSONG ATAU berisi direction yang
    # valid sendiri; kalau berisi teks lain (mis. "Bullish Setup"), itu
    # header dekoratif sungguhan -> serahkan ke LLM fallback.
    header_parts = first_line.split("|")
    symbol_candidate = header_parts[0].strip()
    remainder = "|".join(header_parts[1:]).strip()

    # Buang keterangan dalam kurung DULU, mis. "US30 (Dow Jones)" -> "US30",
    # sebelum cek spasi tersisa (supaya tidak salah dianggap simbol+direction
    # crammed di baris yang sama).
    symbol_candidate = re.sub(r"\s*\([^)]*\)\s*$", "", symbol_candidate).strip()

    # Tanpa "|" sama sekali tapi baris simbol masih berisi >1 token (mis.
    # "spx sell below 6548") — coba pisah: token pertama simbol, sisanya
    # kandidat direction.
    if not remainder and " " in symbol_candidate:
        first_token, _, rest = symbol_candidate.partition(" ")
        if SYMBOL_RE.match(first_token):
            symbol_candidate, remainder = first_token, rest.strip()

    if remainder and not (DIRECTION_RE.search(remainder) or MARKET_DIRECTION_RE.search(remainder)):
        return None

    if not SYMBOL_RE.match(symbol_candidate):
        return None
    symbol = symbol_candidate.upper()

    entry_low = None
    entry_high = None

    direction_match = DIRECTION_RE.search(text)
    if direction_match:
        action = direction_match.group(1).upper()
        entry_low = _to_float(direction_match.group(2))
        if entry_low is None:
            return None
        if direction_match.group(3) is not None:
            entry_high = _to_float(direction_match.group(3))
            if entry_high is None:
                return None  # capture ada tapi gagal parse -> jangan asal pakai entry_low doang
    else:
        # Tidak ada angka entry sama sekali -> coba pola "Buy/Sell Now"
        # polos (market order, tanpa level spesifik).
        market_match = MARKET_DIRECTION_RE.search(text)
        if not market_match:
            return None
        action = market_match.group(1).upper()

    sl_match = SL_LINE_RE.search(text)
    if not sl_match:
        return None
    sl = _to_float(sl_match.group(1))
    if sl is None:
        return None

    tp_match = TP_LINE_RE.search(text)
    if not tp_match:
        return None
    tp = _parse_number_list(tp_match.group(1))
    if not tp:
        return None

    # Validasi arah SL/TP relatif ke entry — channel kadang salah ketik
    # angka (mis. kelebihan digit: "50670" padahal maksudnya "5067") yang
    # bikin SL/TP ada di sisi yang matematis tidak masuk akal untuk arah
    # tradenya. entry_low dipakai sebagai acuan (berlaku utk entry tunggal
    # maupun rentang). Kalau tidak ada level entry sama sekali (market
    # order polos "Buy/Sell Now"), validasi ini dilewati karena tidak ada
    # angka acuan dari teks.
    if entry_low is not None:
        if action == "BUY":
            if sl >= entry_low:
                return None  # SL harus di BAWAH entry untuk BUY -- data tidak valid
            tp = [t for t in tp if t > entry_low]
        else:
            if sl <= entry_low:
                return None  # SL harus di ATAS entry untuk SELL -- data tidak valid
            tp = [t for t in tp if t < entry_low]
        if not tp:
            return None

    if entry_high is not None:
        return Signal(
            message_id=message_id,
            action=action,
            symbol=symbol,
            entry=None,
            entry_range=(entry_low, entry_high),
            sl=sl,
            tp=tp,
        )

    return Signal(
        message_id=message_id,
        action=action,
        symbol=symbol,
        entry=entry_low,
        entry_range=None,
        sl=sl,
        tp=tp,
    )
