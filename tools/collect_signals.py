"""Fase 1: listener yang HANYA mencatat pesan channel — tidak trading.

    python tools/collect_signals.py

Jalankan ini 3-5 hari untuk mengumpulkan korpus signal asli. Setiap pesan
masuk dicatat ke SQLite (store/bot.db) dan di-append ke
tests/fixtures/signals.jsonl, dasar untuk menulis parser di Fase 2.

Prasyarat: tools/login_telegram.py sudah pernah dijalankan (session ada),
dan config/settings.yaml -> telegram.channel sudah diisi.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient, events

from src.store.db import Database

load_dotenv(dotenv_path="config/.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_signals")

DB_PATH = "store/bot.db"
FIXTURE_PATH = "tests/fixtures/signals.jsonl"
EDITS_FIXTURE_PATH = "tests/fixtures/edits.jsonl"


def load_config():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def append_fixture(row: dict, path: str = FIXTURE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


async def main():
    config = load_config()
    channel = config["telegram"]["channel"]
    if not channel:
        raise SystemExit(
            "Isi telegram.channel di config/settings.yaml dulu "
            "(username channel, mis. \"@nama_channel\", atau ID numerik)."
        )

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH belum diisi di config/.env")

    session_path = config["telegram"].get("session_name", "session/user")
    if not os.path.exists(f"{session_path}.session"):
        raise SystemExit(
            f"Session {session_path}.session tidak ditemukan. "
            "Jalankan dulu: python tools/login_telegram.py"
        )

    db = Database(DB_PATH)
    db.init_schema()

    client = TelegramClient(session_path, int(api_id), api_hash)

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        msg = event.message
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
        inserted = db.insert_message(row)
        if inserted:
            preview = text[:120].replace("\n", " | ")
            log.info("Pesan baru #%s: %s", msg.id, preview)
            append_fixture(row)
        else:
            log.debug("Pesan duplikat #%s diabaikan", msg.id)

    @client.on(events.MessageEdited(chats=channel))
    async def edit_handler(event):
        # Banyak channel signal meng-edit pesan asli untuk update
        # ("TP1 hit", "SL to BE") alih-alih kirim pesan baru — ini penting
        # untuk desain follow-up parser di Fase 2, jadi dicatat terpisah.
        msg = event.message
        text = msg.raw_text or ""
        row = {
            "message_id": msg.id,
            "channel": str(channel),
            "text": text,
            "edited_at_utc": msg.edit_date.astimezone(timezone.utc).isoformat() if msg.edit_date else None,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        db.insert_edit(row)
        preview = text[:120].replace("\n", " | ")
        log.info("Pesan #%s di-edit: %s", msg.id, preview)
        append_fixture(row, EDITS_FIXTURE_PATH)

    log.info("Mendengarkan channel: %s", channel)
    log.info("Total pesan tercatat sejauh ini: %d", db.count_messages())
    await client.start()
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
