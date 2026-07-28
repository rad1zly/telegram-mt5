"""Kirim notifikasi ke Telegram pribadi (bot dari @BotFather), bukan ke
channel signal. Dipakai main.py untuk laporan real-time: signal
terdeteksi, order sukses/gagal, follow-up diterapkan, dst.

Kalau TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi, notifikasi cuma
di-log (tidak crash) — supaya bagian lain pipeline tetap bisa dites tanpa
setup notifier dulu.
"""

import logging
import os

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi — notifikasi dilewati: %s", text)
        return

    try:
        resp = requests.post(
            API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Gagal kirim notifikasi Telegram (%s): %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Error kirim notifikasi Telegram: %s", e)
