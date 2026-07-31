"""Klasifikasi SELURUH korpus tests/fixtures/signals.jsonl lewat
classify_message_with_llm (MiniMax) -- SEKALI jalan, hasilnya disimpan ke
cache JSONL supaya backtest bisa dipakai ULANG-ULANG tanpa panggil API lagi
(lihat tools/run_backtest.py --source llm).

    .venv/bin/python tools/llm_classify_corpus.py
    (atau .venv\\Scripts\\python.exe di Windows)

BUTUH MINIMAX_API_KEY terisi di config/.env. Ini akan makan waktu CUKUP
LAMA (rate limit tergantung paket MiniMax kamu -- di paket dasar, replay
~6700 pesan bisa 1.5-2 jam) dan levels billing (bukan gratis). Progress
disimpan INCREMENTAL ke cache -- aman diberhentikan (Ctrl+C) kapan saja dan
dilanjutkan lagi nanti, TIDAK akan re-classify message yang sudah selesai
(hemat biaya kalau perlu resume).
"""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(dotenv_path="config/.env")

from src.parser.llm_fallback import DEFAULT_MODEL, MINIMAX_BASE_URL, classify_message_with_llm  # noqa: E402
from src.parser.schema import FollowUp, Signal  # noqa: E402

CORPUS_PATH = "tests/fixtures/signals.jsonl"
CACHE_PATH = "backtest/data/llm_classify_cache.jsonl"
MAX_WORKERS = 8  # naikkan hati-hati -- terlalu tinggi kena 429 rate limit


def _serialize(result) -> dict:
    if result is None:
        return {"type": "none"}
    if isinstance(result, Signal):
        return {
            "type": "signal", "action": result.action, "symbol": result.symbol,
            "entry": result.entry,
            "entry_range": list(result.entry_range) if result.entry_range else None,
            "sl": result.sl, "tp": result.tp,
        }
    if isinstance(result, FollowUp):
        return {"type": "followup", "kinds": result.kinds, "symbol": result.symbol}
    return {"type": "unknown"}


def main():
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("MINIMAX_API_KEY belum diisi di config/.env -- berhenti.")
        return

    client = OpenAI(api_key=api_key, base_url=MINIMAX_BASE_URL, max_retries=10, timeout=60.0)

    rows = []
    with open(CORPUS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    already_done = set()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    already_done.add(json.loads(line)["message_id"])

    todo = [r for r in rows if r["message_id"] not in already_done and (r.get("text") or "").strip()]
    print(f"Total korpus: {len(rows)}, sudah selesai: {len(already_done)}, sisa: {len(todo)}")
    if not todo:
        print("Semua pesan sudah terklasifikasi -- tidak ada yang perlu dijalankan.")
        return

    lock = threading.Lock()
    cache_file = open(CACHE_PATH, "a", encoding="utf-8")
    count = 0
    start = time.time()

    def process(row):
        text = row["text"]
        try:
            result = classify_message_with_llm(
                text, message_id=row["message_id"], reply_to_msg_id=row.get("reply_to_msg_id"),
                client=client, model=DEFAULT_MODEL,
            )
        except Exception:
            result = None
        return row["message_id"], _serialize(result)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process, row): row for row in todo}
        for fut in as_completed(futures):
            mid, data = fut.result()
            data["message_id"] = mid
            with lock:
                cache_file.write(json.dumps(data) + "\n")
                cache_file.flush()
                count += 1
                if count % 50 == 0:
                    elapsed = time.time() - start
                    rate = count / elapsed
                    remaining = (len(todo) - count) / rate if rate > 0 else 0
                    print(f"  {count}/{len(todo)} selesai ({elapsed/60:.1f} menit berlalu, estimasi sisa {remaining/60:.1f} menit)")

    cache_file.close()
    print(f"\nSELESAI. Cache tersimpan di {CACHE_PATH}")
    print("Jalankan: .venv/bin/python tools/run_backtest.py --source llm")


if __name__ == "__main__":
    main()
