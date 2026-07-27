# Telegram Signal → MT5 Autotrade

Status: **Fase 1 — listen-only recorder**. Belum ada trading. Bot hanya
membaca channel dan mencatat pesan sebagai korpus untuk menulis parser di
Fase 2. Lihat plan lengkap di `~/.claude/plans/jadi-aku-tuh-punya-humming-rain.md`.

## Setup

1. Buat virtualenv dan install dependency:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Salin `config/.env.example` ke `config/.env` dan isi:
   - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — dari https://my.telegram.org
     (API development tools)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — buat bot lewat @BotFather,
     lalu kirim satu pesan ke bot itu dan cek `chat_id` lewat
     `https://api.telegram.org/bot<TOKEN>/getUpdates`

3. Isi `telegram.channel` di `config/settings.yaml` dengan username
   channel (mis. `"@nama_channel"`) atau ID numeriknya.

4. Login ke Telegram (sekali saja, interaktif — minta nomor HP + kode OTP):

   ```bash
   python tools/login_telegram.py
   ```

   Ini membuat `session/user.session`. Jangan commit atau share file ini —
   isinya setara dengan akses penuh ke akun Telegram kamu.

5. Jalankan recorder:

   ```bash
   python tools/collect_signals.py
   ```

   Biarkan jalan 3–5 hari. Setiap pesan (baru maupun yang di-edit) dicatat
   ke `store/bot.db` dan `tests/fixtures/signals.jsonl` /
   `tests/fixtures/edits.jsonl`.

## Struktur

```
config/          settings.yaml, .env (kredensial, gitignored)
src/store/       SQLite layer (messages, signals, positions, followups)
src/parser/      schema.py — kontrak Signal/FollowUp untuk Fase 2
tools/           script yang kamu jalankan manual (login, recorder)
tests/fixtures/  korpus signal asli hasil recorder — dasar Fase 2
```

## Fase berikutnya

Setelah korpus `tests/fixtures/signals.jsonl` cukup (beragam kasus:
entry biasa, multi-TP, follow-up), lanjut ke Fase 2: menulis
`src/parser/patterns.py` dan `src/parser/followup.py`, diuji terhadap
korpus ini sebelum menyentuh eksekusi MT5.

## Yang perlu kamu lakukan sekarang

- [ ] Isi `config/.env` (API_ID, API_HASH, BOT_TOKEN, CHAT_ID)
- [ ] Isi `telegram.channel` di `config/settings.yaml`
- [ ] Jalankan `python tools/login_telegram.py` dan masukkan OTP
- [ ] Jalankan `python tools/collect_signals.py` dan biarkan menyala
