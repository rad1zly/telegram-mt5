"""Baca cache hasil classify_message_with_llm (JSONL, dibuat oleh
tools/llm_classify_corpus.py) dan sediakan classify_fn yang kompatibel
dengan backtest/runner.py & backtest/tick_runner.py -- supaya backtest bisa
dijalankan pakai keputusan LLM (bukan regex) TANPA panggil API lagi
(sudah dicache sekali, backtest ulang-ulang gratis)."""

import json
from typing import Optional

from src.parser.schema import FollowUp, Signal


def load_llm_cache(path: str) -> dict:
    """{message_id: record} dari file JSONL. Baris yang message_id-nya
    dobel (mis. hasil resume job yang sempat diulang) -- yang PALING AKHIR
    di file menang (overwrite), konsisten dgn semantik "cache terbaru"."""
    cache: dict = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cache[row["message_id"]] = row
    return cache


def make_llm_classify_fn(cache: dict):
    """Return classify_fn(text, message_id, reply_to_msg_id) -> Signal |
    FollowUp | None, sesuai kontrak yang dipakai backtest/runner.py:run()
    dan backtest/tick_runner.py:run() lewat parameter classify_fn."""

    def classify_fn(text: str, message_id: int, reply_to_msg_id: Optional[int]):
        record = cache.get(message_id)
        if record is None:
            # Belum terklasifikasi (mis. job classify_message_with_llm belum
            # sampai situ) -- perlakukan sama dgn "LLM tidak panggil tool",
            # BUKAN dianggap error.
            return None

        rtype = record.get("type")
        if rtype == "signal":
            entry_range = tuple(record["entry_range"]) if record.get("entry_range") else None
            return Signal(
                message_id=message_id,
                action=record["action"],
                symbol=record["symbol"],
                entry=record.get("entry"),
                entry_range=entry_range,
                sl=record.get("sl"),
                tp=record.get("tp") or [],
            )
        if rtype == "followup":
            return FollowUp(
                message_id=message_id,
                reply_to_msg_id=reply_to_msg_id,
                kinds=record.get("kinds") or [],
                raw_text=text,
                symbol=record.get("symbol"),
            )
        return None  # rtype == "none" atau tidak dikenal

    return classify_fn
