"""Fallback parser via MiniMax (API kompatibel format OpenAI) — dipanggil
HANYA kalau regex di patterns.py / followup.py gagal mengenali pesan.

Prinsip sama dengan symbols.py: kalau model tidak yakin, jangan menebak.
Tool-calling dipakai untuk memaksa jawaban terstruktur; kalau model tidak
memanggil tool sama sekali, atau field wajib kosong, hasilnya ditolak
(return None) dan signal itu diteruskan sebagai notifikasi "tidak
terparsing" ke user alih-alih dieksekusi.
"""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

from src.parser.schema import FollowUp, Signal

log = logging.getLogger(__name__)

MINIMAX_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-M2.7-highspeed"  # non-agentic, latensi rendah — cukup untuk ekstraksi terstruktur

SIGNAL_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_signal",
        "description": (
            "Ekstrak detail order trading dari teks signal. Panggil tool ini "
            "HANYA jika teks berisi INSTRUKSI KONKRET buka posisi (arah + level "
            "entry + stop loss yang jelas), bahkan kalau instruksi itu dibungkus "
            "dalam paragraf analisa dengan judul dekoratif (mis. 'GOLD | Bullish "
            "Setup', 'GOLD | Bearish Momentum'). Cari kalimat yang berbentuk "
            "arahan langsung ('Buy Above X', 'Sell Below X', kadang didahului "
            "label seperti 'So.:'), BUKAN kalimat penalaran hipotetis semata "
            "('as long as price remains below X, expected to move toward Y' "
            "TANPA arahan buka posisi eksplisit = itu cuma outlook, jangan "
            "panggil tool). Kalau teks ambigu atau tidak ada arahan konkret — "
            "JANGAN panggil tool apa pun, biar diteruskan sebagai notifikasi "
            "manual, bukan ditebak."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": (
                        "HANYA arah (BUY/SELL) -- JANGAN pernah kembalikan varian "
                        "BUY_LIMIT/SELL_LIMIT/BUY_STOP/SELL_STOP walau teks channel "
                        "menyebut 'buy limit'/'sell stop', dst. Market order vs pending "
                        "(dan STOP vs LIMIT kalau pending) diputuskan kode eksekusi "
                        "sendiri dengan membandingkan entry ke harga live saat itu, "
                        "BUKAN dari teks -- kode hilir HANYA mengenali string 'BUY'/'SELL' "
                        "persis, varian lain akan salah diproses."
                    ),
                },
                "symbol": {
                    "type": "string",
                    "description": "Simbol persis seperti disebut di teks, mis. GOLD, XAUUSD, NAS100, EURUSD",
                },
                "entry": {"type": "number", "description": "Harga entry tunggal. Kosongkan jika entry berupa rentang."},
                "entry_range_low": {"type": "number"},
                "entry_range_high": {"type": "number"},
                "sl": {
                    "type": "number",
                    "description": (
                        "Stop loss — wajib ada. Kalau SL ditulis sebagai kondisi candle-close "
                        "(mis. 'stop loss 4340 or 15min close below 4341'), pakai ANGKA HARGA yang "
                        "disebut (4341) sebagai SL — ini penyederhanaan yang disengaja karena bot "
                        "tidak memantau candle secara live, jadi dipakai sebagai harga stop tetap."
                    ),
                },
                "tp": {"type": "array", "items": {"type": "number"}, "description": "Daftar take profit."},
            },
            "required": ["action", "symbol", "sl"],
        },
    },
}

FOLLOWUP_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_followup",
        "description": (
            "Klasifikasikan pesan susulan terkait posisi yang sudah terbuka. "
            "Sebuah pesan bisa berisi LEBIH DARI SATU instruksi sekaligus "
            "(mis. 'close partially AND move SL to entry'), jadi kinds adalah "
            "daftar. Kalau pesan cuma update info murni (mis. skenario "
            "kondisional 'kalau breakout di atas/bawah X akan begini', atau "
            "narasi 'Hit SL -X pip' yang memang sudah otomatis tereksekusi "
            "lewat order SL broker, tidak butuh aksi baru), kembalikan kinds "
            "kosong []. Jangan longgarkan kategori demi memaksa cocok."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Simbol/instrumen yang dirujuk pesan ini, kalau disebut.",
                },
                "kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["move_sl_be", "partial_close_tp1", "close_all"],
                    },
                    "description": (
                        "PRINSIP PENTING (dipelajari dari ratusan contoh nyata korpus channel ini):\n\n"
                        "1. 'Hit Target'/'Hit Profit'/'Hit Second Target' adalah HEADLINE PENANDA "
                        "PROGRES, BUKAN instruksi tutup posisi dengan sendirinya -- channel ini "
                        "sering punya BEBERAPA target berurutan (Hit Target pertama baru separuh "
                        "jalan, bukan akhir trade). JANGAN otomatis close_all cuma karena ada "
                        "headline ini -- lihat instruksi KONKRET di badan pesan.\n\n"
                        "2. move_sl_be dipicu kalau SL dipindah/ditaruh ('move'/'place') ke ENTRY "
                        "atau BREAKEVEN secara eksplisit. Kalau instruksinya harga BARU yang "
                        "SPESIFIK dan BUKAN entry (mis. 'place your sl around 4349'), JANGAN "
                        "masukkan move_sl_be -- kita tidak bisa aman auto-set harga SL sembarang "
                        "tanpa tahu harga fill kita sendiri, biarkan kinds kosong untuk bagian ini.\n\n"
                        "3. partial_close_tp1 dipicu oleh instruksi 'close partial(ly)' yang jelas, "
                        "ATAU oleh bahasa 'secure/protect profit(s)' TANPA kata close eksplisit "
                        "(mis. 'we recommend securing your profits') -- di korpus ini, frasa itu "
                        "hampir selalu jadi alasan/pelengkap instruksi partial, BUKAN perintah "
                        "tutup penuh berdiri sendiri. Kalimat PILIHAN eksplisit ('close fully OR "
                        "partially') diresolve ke partial_close_tp1 (opsi lebih konservatif -- tetap "
                        "mengunci sebagian profit tanpa menutup penuh posisi yang mungkin masih "
                        "berjalan), BUKAN dianggap ambigu lalu didiamkan.\n\n"
                        "4. close_all dipicu oleh instruksi tutup PENUH yang eksplisit ('close the "
                        "position/all/fully/positions', TANPA kata partial), ATAU oleh narasi bentuk "
                        "LAMPAU 'Closed the position/trade' (channel bilang mereka SUDAH cut-loss/"
                        "tutup posisi sendiri -- ikuti keputusan real-time itu, jangan biarkan "
                        "posisi kita menunggu SL asli yang lebih jauh). HATI-HATI: 'closed a 15min "
                        "candle above/below X' itu soal CANDLE, bukan soal posisi -- JANGAN salah "
                        "anggap sebagai close_all.\n\n"
                        "Kosongkan array kalau benar-benar tidak ada kategori yang cocok."
                    ),
                },
            },
            "required": ["kinds"],
        },
    },
}

SIGNAL_SYSTEM_PROMPT = (
    "Kamu mengekstrak signal trading (forex/gold/index) dari pesan channel "
    "Telegram. Channel ini kadang menulis signal dalam format baku (simbol "
    "polos di baris pertama, lalu Buy/Sell + level, Tp:, Sl:), dan kadang "
    "dalam paragraf analisa berjudul dekoratif ('Bullish Setup', 'Bearish "
    "Momentum', 'Bullish Continuation') yang isinya BISA murni outlook/opini "
    "TANPA instruksi buka posisi, ATAU bisa juga menyisipkan instruksi "
    "konkret di tengah paragraf. Bedakan keduanya dari ada-tidaknya arahan "
    "eksplisit (arah + level entry + SL yang jelas), bukan dari judulnya. "
    "Jangan berhalusinasi angka yang tidak ada di teks."
)
FOLLOWUP_SYSTEM_PROMPT = (
    "Kamu mengklasifikasikan pesan susulan trading dari channel Telegram "
    "yang merujuk ke posisi yang sudah dibuka sebelumnya."
)
COMBINED_SYSTEM_PROMPT = (
    "Kamu adalah otak pengambilan keputusan bot trading otomatis yang membaca "
    "SATU pesan dari channel sinyal trading Telegram, persis seperti trader "
    "manusia yang memantau channel ini akan membacanya. Setiap pesan bisa jadi: "
    "(a) SIGNAL BARU (instruksi buka posisi), (b) FOLLOW-UP (merujuk posisi yang "
    "sudah terbuka -- pindah SL, partial close, close all, atau cuma update info), "
    "atau (c) BUKAN KEDUANYA (berita pasar, hasil mingguan, promosi, obrolan biasa). "
    "Panggil PALING BANYAK SATU tool yang cocok. Kalau (c), atau kalau ragu sama "
    "sekali kategori mana yang cocok, JANGAN panggil tool apa pun -- diam lebih "
    "aman daripada menebak salah dengan uang sungguhan. Jangan berhalusinasi "
    "angka yang tidak ada di teks."
)


def _client() -> OpenAI:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY belum diisi di config/.env")
    return OpenAI(api_key=api_key, base_url=MINIMAX_BASE_URL)


def _first_tool_call_args(response) -> Optional[dict]:
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError):
        return None
    if not getattr(message, "tool_calls", None):
        return None
    try:
        return json.loads(message.tool_calls[0].function.arguments)
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as e:
        log.error("Gagal parse argumen tool dari MiniMax: %s", e)
        return None


def _first_tool_call(response) -> Optional[tuple[str, dict]]:
    """Sama seperti _first_tool_call_args, tapi juga kembalikan NAMA tool
    yang dipanggil -- dipakai classify_message_with_llm untuk membedakan
    model memanggil extract_signal vs extract_followup."""
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError):
        return None
    if not getattr(message, "tool_calls", None):
        return None
    call = message.tool_calls[0]
    try:
        args = json.loads(call.function.arguments)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        log.error("Gagal parse argumen tool dari MiniMax: %s", e)
        return None
    return call.function.name, args


def parse_signal_with_llm(
    text: str,
    message_id: int,
    client: Optional[OpenAI] = None,
    model: str = DEFAULT_MODEL,
) -> Optional[Signal]:
    client = client or _client()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SIGNAL_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=[SIGNAL_TOOL],
            tool_choice="auto",
        )
    except Exception as e:
        log.error("MiniMax API error saat parsing signal #%s: %s", message_id, e)
        return None

    args = _first_tool_call_args(response)
    if args is None:
        log.info("LLM tidak yakin ada signal di pesan #%s — dilewati", message_id)
        return None

    action = args.get("action")
    symbol = args.get("symbol")
    sl = args.get("sl")
    if not action or not symbol or sl is None:
        log.info("Hasil LLM untuk pesan #%s tidak lengkap (action/symbol/sl kosong) — ditolak", message_id)
        return None

    entry_range = None
    if args.get("entry_range_low") is not None and args.get("entry_range_high") is not None:
        entry_range = (args["entry_range_low"], args["entry_range_high"])

    return Signal(
        message_id=message_id,
        action=action,
        symbol=symbol,
        entry=args.get("entry"),
        entry_range=entry_range,
        sl=sl,
        tp=args.get("tp") or [],
    )


def parse_followup_with_llm(
    text: str,
    message_id: int,
    reply_to_msg_id: Optional[int],
    client: Optional[OpenAI] = None,
    model: str = DEFAULT_MODEL,
) -> Optional[FollowUp]:
    client = client or _client()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": FOLLOWUP_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=[FOLLOWUP_TOOL],
            tool_choice="auto",
        )
    except Exception as e:
        log.error("MiniMax API error saat parsing follow-up #%s: %s", message_id, e)
        return None

    args = _first_tool_call_args(response)
    if args is None:
        # Model tidak memanggil tool sama sekali -> tidak yakin ini follow-up terkait
        log.info("LLM tidak yakin pesan #%s adalah follow-up — dilewati", message_id)
        return None

    # kinds=[] valid di sini: berarti follow-up dikenali tapi tidak ada aksi
    # otomatis yang cocok (info-only) — beda dari args is None (tidak yakin sama sekali).
    return FollowUp(
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
        kinds=args.get("kinds") or [],
        raw_text=text,
        symbol=args.get("symbol"),
    )


def classify_message_with_llm(
    text: str,
    message_id: int,
    reply_to_msg_id: Optional[int],
    client: Optional[OpenAI] = None,
    model: str = DEFAULT_MODEL,
) -> "Optional[Signal | FollowUp]":
    """MODE TRIAL "LLM-first": SATU panggilan LLM per pesan, dengan KEDUA
    tool (extract_signal, extract_followup) sekaligus ditawarkan -- model
    sendiri yang memutuskan pesan ini signal baru, follow-up, atau bukan
    keduanya (tidak panggil tool sama sekali -> return None, diteruskan
    sebagai notifikasi/diabaikan seperti biasa, TIDAK ada eksekusi).

    Beda dari parse_signal_with_llm/parse_followup_with_llm (yang masing-
    masing HANYA dipanggil sebagai fallback setelah regex gagal): fungsi
    ini dipakai ketika parser.llm_first=true di config, menggantikan regex
    sebagai pengambil keputusan UTAMA untuk setiap pesan yang masuk."""
    client = client or _client()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=[SIGNAL_TOOL, FOLLOWUP_TOOL],
            tool_choice="auto",
        )
    except Exception as e:
        log.error("MiniMax API error saat classify_message #%s: %s", message_id, e)
        return None

    result = _first_tool_call(response)
    if result is None:
        log.info("LLM tidak memanggil tool apa pun untuk pesan #%s — diabaikan", message_id)
        return None
    name, args = result

    if name == "extract_signal":
        action = args.get("action")
        symbol = args.get("symbol")
        sl = args.get("sl")
        if not action or not symbol or sl is None:
            log.info("LLM panggil extract_signal utk #%s tapi field wajib kosong — ditolak", message_id)
            return None
        entry_range = None
        if args.get("entry_range_low") is not None and args.get("entry_range_high") is not None:
            entry_range = (args["entry_range_low"], args["entry_range_high"])
        return Signal(
            message_id=message_id, action=action, symbol=symbol,
            entry=args.get("entry"), entry_range=entry_range, sl=sl, tp=args.get("tp") or [],
        )

    if name == "extract_followup":
        return FollowUp(
            message_id=message_id, reply_to_msg_id=reply_to_msg_id,
            kinds=args.get("kinds") or [], raw_text=text, symbol=args.get("symbol"),
        )

    log.error("LLM panggil tool tak dikenal '%s' utk pesan #%s — diabaikan", name, message_id)
    return None
