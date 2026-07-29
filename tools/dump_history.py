"""Ambil SEMUA riwayat pesan channel (bukan cuma pesan baru) — lebih cepat
dari collect_signals.py untuk mempelajari pola signal, karena tidak perlu
menunggu hari demi hari.

    .venv\\Scripts\\python.exe tools\\dump_history.py

Setiap pesan dicatat ke SQLite (sama seperti collect_signals.py, dedup
otomatis) dan di-append ke tests/fixtures/signals.jsonl. Aman dijalankan
berkali-kali — pesan yang sudah tercatat dilewati (unique per message_id).

Catatan: history dump hanya melihat TEKS TERAKHIR tiap pesan. Kalau
sebuah pesan pernah di-edit (mis. entry signal di-edit jadi "TP1 hit"),
yang tersimpan cuma versi editan terakhirnya — riwayat edit sebelumnya
tidak bisa diambil lewat cara ini. Ini beda dengan collect_signals.py
yang menangkap event edit secara terpisah kalau kamu jalankan LIVE saat
edit itu terjadi.

Prasyarat sama seperti collect_signals.py: tools/login_telegram.py sudah
pernah dijalankan, config/settings.yaml -> telegram.channel sudah diisi.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient

from src.store.db import Database
from src.tg.listener import resolve_channel_entity

load_dotenv(dotenv_path="config/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dump_history")

DB_PATH = "store/bot.db"
FIXTURE_PATH = "tests/fixtures/signals.jsonl"


def load_config():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def append_fixture(row: dict) -> None:
    os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
    with open(FIXTURE_PATH, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


async def main():
    config = load_config()
    channel = config["telegram"]["channel"]
    if not channel:
        raise SystemExit("Isi telegram.channel di config/settings.yaml dulu.")

    limit = config["telegram"].get("history_dump_limit")  # None = semua riwayat

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH belum diisi di config/.env")

    session_path = config["telegram"].get("session_name", "session/user")
    if not os.path.exists(f"{session_path}.session"):
        raise SystemExit(f"Session {session_path}.session tidak ada. Jalankan tools/login_telegram.py dulu.")

    db = Database(DB_PATH)
    db.init_schema()

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start()

    log.info("Menyinkronkan daftar chat...")
    try:
        entity = await resolve_channel_entity(client, channel)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"Tidak bisa menemukan channel '{channel}'. Detail: {e}")

    log.info("Channel ditemukan: %s (id=%s)", getattr(entity, "title", channel), entity.id)
    log.info("Mulai dump riwayat (limit=%s)...", limit or "semua")

    total = 0
    inserted_count = 0
    async for msg in client.iter_messages(entity, limit=limit):
        total += 1
        text = msg.raw_text or ""
        row = {
            "message_id": msg.id,
            "channel": str(channel),
            "date_utc": msg.date.astimezone(timezone.utc).isoformat(),
            "text": text,
            "reply_to_msg_id": msg.reply_to_msg_id,
            "raw_json": json.dumps(msg.to_dict(), default=str),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        if db.insert_message(row):
            inserted_count += 1
            append_fixture(row)

        if total % 100 == 0:
            log.info("Progres: %d pesan diproses, %d baru", total, inserted_count)

    log.info("Selesai. Total diproses: %d, baru disimpan: %d (sisanya duplikat/sudah ada)", total, inserted_count)
    log.info("Total pesan tercatat di DB sekarang: %d", db.count_messages())

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
