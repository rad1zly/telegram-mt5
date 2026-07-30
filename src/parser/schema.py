from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    """Hasil parsing pesan entry signal.

    action cuma arah (BUY/SELL) — sengaja TIDAK dibedakan LIMIT vs STOP di
    sini. Channel biasa menulis 'sell below X' / 'sell above X' alih-alih
    istilah order MT5 formal, dan itu ambigu untuk ditebak dari kata saja.
    Penentuan market order vs pending (dan STOP vs LIMIT kalau pending)
    dilakukan saat eksekusi (Fase 3), dengan membandingkan entry ke harga
    live saat itu — bukan dari teks.
    """

    message_id: int
    action: str  # BUY | SELL
    symbol: str
    entry: Optional[float] = None
    entry_range: Optional[tuple[float, float]] = None
    sl: Optional[float] = None
    tp: list[float] = field(default_factory=list)


def apply_price_offset(signal: "Signal", offset: float) -> "Signal":
    """Geser SEMUA angka harga (entry, entry_range, sl, tp) sebesar offset
    yang sama -- dipakai kalau broker kita punya selisih harga KONSISTEN
    dan SATU ARAH dari referensi harga channel (mis. broker selalu $10
    lebih tinggi di US30, $7 di NAS100 -- ditemukan lewat perbandingan
    manual harga live). Ini pergeseran PARALEL: jarak relatif entry-ke-SL
    dan entry-ke-TP TIDAK berubah, cuma level absolutnya dikoreksi supaya
    order yang dipasang di broker kita match dengan maksud channel.

    offset positif berarti broker kita LEBIH TINGGI dari referensi channel
    (entry/sl/tp semua ditambah offset)."""
    if offset == 0:
        return signal
    return Signal(
        message_id=signal.message_id,
        action=signal.action,
        symbol=signal.symbol,
        entry=(signal.entry + offset) if signal.entry is not None else None,
        entry_range=(
            (signal.entry_range[0] + offset, signal.entry_range[1] + offset)
            if signal.entry_range is not None
            else None
        ),
        sl=(signal.sl + offset) if signal.sl is not None else None,
        tp=[t + offset for t in signal.tp],
    )


@dataclass
class FollowUp:
    """Pesan susulan yang merujuk ke posisi yang sudah terbuka.

    kinds bisa berisi lebih dari satu aksi sekaligus (contoh nyata: "close
    partially AND move stop-loss to entry" dalam satu pesan) — daftar
    kosong berarti pesan ini info-only, tidak ada aksi otomatis yang
    dikenali (aman, cuma diteruskan sebagai notifikasi).
    """

    message_id: int
    reply_to_msg_id: Optional[int]
    raw_text: str
    kinds: list[str] = field(default_factory=list)  # subset of: move_sl_be, partial_close_tp1, close_all
    symbol: Optional[str] = None  # dipakai untuk mencocokkan ke posisi terbuka kalau tidak ada reply_to_msg_id
