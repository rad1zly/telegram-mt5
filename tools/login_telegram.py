"""Jalankan ini SEKALI, secara manual, untuk membuat session Telethon.

    python tools/login_telegram.py

Akan meminta nomor HP dan kode OTP dari Telegram secara interaktif di
terminal ini. Hasilnya file session/user.session yang dipakai bot
seterusnya — tidak perlu login ulang setelah ini kecuali session dihapus
atau logout dari perangkat Telegram.
"""

import asyncio
import os

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv(dotenv_path="config/.env")


def load_config():
    with open("config/settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main():
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH belum diisi di config/.env\n"
            "Ambil dari https://my.telegram.org -> API development tools"
        )

    config = load_config()
    session_path = config["telegram"].get("session_name", "session/user")
    os.makedirs(os.path.dirname(session_path) or ".", exist_ok=True)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start()  # prompts phone number + OTP here
    me = await client.get_me()
    print(f"Login berhasil sebagai {me.first_name} (@{me.username}).")
    print(f"Session tersimpan di {session_path}.session — jangan commit/share file ini.")

    print("Menyinkronkan daftar chat (supaya channel private bisa dikenali nanti)...")
    dialogs = await client.get_dialogs()
    print(f"Tersinkron {len(dialogs)} chat/channel/grup.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
