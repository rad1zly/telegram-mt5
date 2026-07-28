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
            "HANYA jika teks jelas berisi instruksi buka posisi (arah, simbol, "
            "stop loss). Kalau teks ambigu, cuma commentary, atau bukan signal "
            "entry sama sekali — JANGAN panggil tool apa pun."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"],
                },
                "symbol": {
                    "type": "string",
                    "description": "Simbol persis seperti disebut di teks, mis. GOLD, XAUUSD, NAS100, EURUSD",
                },
                "entry": {"type": "number", "description": "Harga entry tunggal. Kosongkan jika entry berupa rentang."},
                "entry_range_low": {"type": "number"},
                "entry_range_high": {"type": "number"},
                "sl": {"type": "number", "description": "Stop loss — wajib ada."},
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
            "Panggil tool ini HANYA jika pesan jelas berisi instruksi tindak "
            "lanjut. Kalau cuma info tanpa instruksi jelas, pakai kind='info'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["move_sl_be", "partial_close_tp1", "close_all", "info"],
                }
            },
            "required": ["kind"],
        },
    },
}

SIGNAL_SYSTEM_PROMPT = (
    "Kamu mengekstrak signal trading (forex/gold/index) dari pesan channel "
    "Telegram. Jangan berhalusinasi angka yang tidak ada di teks."
)
FOLLOWUP_SYSTEM_PROMPT = (
    "Kamu mengklasifikasikan pesan susulan trading dari channel Telegram "
    "yang merujuk ke posisi yang sudah dibuka sebelumnya."
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
    if args is None or not args.get("kind"):
        log.info("LLM tidak yakin jenis follow-up di pesan #%s — dilewati", message_id)
        return None

    return FollowUp(
        message_id=message_id,
        reply_to_msg_id=reply_to_msg_id,
        kind=args["kind"],
        raw_text=text,
    )
