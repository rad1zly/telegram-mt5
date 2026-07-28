"""Pipeline penuh: listener Telegram -> parser -> eksekusi MT5 -> notifikasi.

    .venv\\Scripts\\python.exe src\\main.py

WAJIB config/settings.yaml -> mode: demo, dan akun yang login di terminal
MT5 WAJIB benar-benar demo (dicek dari account_info, bukan cuma config) —
kalau tidak, proses berhenti sebelum sempat listen sama sekali.

Alur per pesan baru:
  1. Dedup (SQLite, unique per message_id) — pesan yang sama diabaikan.
  2. Coba parse sebagai entry signal (regex, lalu fallback MiniMax kalau
     MINIMAX_API_KEY ada). Kalau berhasil -> resolve simbol, hitung lot
     dari risiko tetap, kirim order, simpan posisi, notifikasi.
  3. Kalau bukan entry, coba parse sebagai follow-up "Live Update" (regex,
     lalu fallback MiniMax). Kalau ada kinds yang cocok (move_sl_be /
     partial_close_tp1) DAN diaktifkan di config, terapkan ke posisi
     terbuka yang match simbolnya. close_all tidak pernah dieksekusi
     otomatis, cuma notifikasi (lihat plan).
  4. Bukan keduanya -> diabaikan (banyak chatter non-signal di channel).

Guard yang AKTIF: dedup, SL wajib ada, simbol harus ke-resolve jelas, lot
harus lolos volume_min, max_trades_per_day. Guard yang BELUM ada (spread,
deviasi harga live, daily_loss_cap) — lihat plan Fase 3/4, menyusul.
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
from telethon import TelegramClient, events

from src.parser.followup import parse_followup_regex
from src.parser.llm_fallback import parse_followup_with_llm, parse_signal_with_llm
from src.parser.patterns import parse_entry_signal
from src.store.db import Database
from src.tg import notifier
from src.tg.listener import resolve_channel_entity
from src.trading import mt5_client
from src.trading.executor import execute_signal
from src.trading.symbols import SymbolResolver

load_dotenv(dotenv_path="config/.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")

DB_PATH = "store/bot.db"


def load_settings():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def llm_available() -> bool:
    return bool(os.environ.get("MINIMAX_API_KEY"))


def today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class Context:
    def __init__(self, settings: dict, db: Database, resolver: SymbolResolver, broker_symbols: list):
        self.settings = settings
        self.db = db
        self.resolver = resolver
        self.broker_symbols = broker_symbols


async def handle_entry_signal(ctx: Context, signal, msg) -> None:
    max_per_day = ctx.settings["risk"]["max_trades_per_day"]
    opened_today = ctx.db.count_positions_opened_since(today_start_iso())
    if opened_today >= max_per_day:
        notifier.send(
            f"⏸️ Signal #{msg.id} ({signal.symbol}) dilewati — sudah {opened_today} "
            f"trade hari ini, batas max_trades_per_day={max_per_day}."
        )
        return

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: execute_signal(
            signal=signal,
            resolver=ctx.resolver,
            broker_symbols=ctx.broker_symbols,
            risk_usd=ctx.settings["risk"]["usd_per_trade"],
            max_lot_cap=ctx.settings["risk"]["max_lot_cap"],
            max_price_deviation_pips=ctx.settings["guards"]["max_price_deviation_pips"],
            price_deviation_overrides=ctx.settings["guards"].get("price_deviation_overrides"),
        ),
    )

    if not result.success:
        notifier.send(f"⚠️ Signal #{msg.id} ({signal.symbol}) GAGAL dieksekusi:\n{result.detail}")
        return

    ctx.db.insert_position({
        "signal_id": msg.id,
        "ticket": result.ticket,
        "symbol": signal.symbol,
        "lot": result.lot,
        "open_price": result.price,
        "sl": signal.sl,
        "tp": signal.tp[-1] if signal.tp else None,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
    })
    notifier.send(f"✅ Signal #{msg.id} dieksekusi:\n{result.detail}")


async def handle_followup(ctx: Context, followup, msg) -> None:
    if not followup.kinds:
        preview = followup.raw_text[:200].replace("\n", " | ")
        notifier.send(f"ℹ️ Update #{msg.id} ({followup.symbol or '?'}): {preview}")
        return

    if followup.symbol is None:
        notifier.send(f"⚠️ Follow-up #{msg.id} tidak jelas simbolnya — dilewati, cek manual.")
        return

    position = ctx.db.get_open_position_by_symbol(followup.symbol)
    if position is None:
        notifier.send(f"⚠️ Follow-up #{msg.id} ({followup.symbol}) — tidak ada posisi terbuka yang cocok, dilewati.")
        return

    loop = asyncio.get_event_loop()
    resolved = ctx.resolver.resolve(followup.symbol, ctx.broker_symbols)
    if not resolved.ok:
        notifier.send(f"⚠️ Follow-up #{msg.id}: simbol {followup.symbol} tidak ke-resolve ({resolved.error})")
        return
    broker_symbol = resolved.matched

    if "move_sl_be" in followup.kinds and not position["be_moved"]:
        if ctx.settings["followup"]["move_sl_to_be"] and position["open_price"] is not None:
            result = await loop.run_in_executor(
                None,
                lambda: mt5_client.modify_sl_tp(position["ticket"], broker_symbol, sl=position["open_price"]),
            )
            if result.success:
                ctx.db.mark_be_moved(position["id"])
                notifier.send(f"✅ SL posisi #{position['ticket']} ({followup.symbol}) dipindah ke breakeven")
            else:
                notifier.send(f"⚠️ Gagal pindah SL ke BE #{position['ticket']}: {result.error}")

    if "partial_close_tp1" in followup.kinds and not position["tp1_hit"]:
        if ctx.settings["followup"]["partial_close_tp1"] and position["lot"]:
            pct = ctx.settings["followup"]["partial_close_percent"] / 100
            close_volume = round(position["lot"] * pct, 2)
            if close_volume > 0:
                result = await loop.run_in_executor(
                    None,
                    lambda: mt5_client.partial_close(position["ticket"], broker_symbol, close_volume),
                )
                if result.success:
                    ctx.db.mark_tp1_hit(position["id"])
                    notifier.send(f"✅ Partial close {close_volume} lot #{position['ticket']} ({followup.symbol})")
                else:
                    notifier.send(f"⚠️ Gagal partial close #{position['ticket']}: {result.error}")

    if "close_all" in followup.kinds:
        notifier.send(
            f"ℹ️ Channel minta CLOSE ALL untuk {followup.symbol} (#{position['ticket']}) — "
            f"TIDAK dieksekusi otomatis (close_all sengaja off). Cek manual kalau perlu."
        )


async def handle_new_message(ctx: Context, channel_label: str, msg) -> None:
    text = msg.raw_text or ""
    row = {
        "message_id": msg.id,
        "channel": channel_label,
        "date_utc": msg.date.astimezone(timezone.utc).isoformat(),
        "text": text,
        "reply_to_msg_id": msg.reply_to_msg_id,
        "raw_json": json.dumps(msg.to_dict(), default=str),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    if not ctx.db.insert_message(row):
        log.debug("Pesan duplikat #%s diabaikan", msg.id)
        return

    signal = parse_entry_signal(text, message_id=msg.id)
    if signal is None and llm_available():
        signal = parse_signal_with_llm(text, message_id=msg.id)

    if signal is not None:
        await handle_entry_signal(ctx, signal, msg)
        return

    followup = parse_followup_regex(text, message_id=msg.id, reply_to_msg_id=msg.reply_to_msg_id)
    if followup is None and llm_available():
        followup = parse_followup_with_llm(text, message_id=msg.id, reply_to_msg_id=msg.reply_to_msg_id)

    if followup is not None:
        await handle_followup(ctx, followup, msg)
        return

    log.info("Pesan #%s bukan entry/follow-up yang dikenali — diabaikan", msg.id)


async def main():
    settings = load_settings()

    if settings.get("mode") != "demo":
        raise SystemExit(
            f"config/settings.yaml -> mode = '{settings.get('mode')}', bukan 'demo'. "
            "Pipeline ini menolak jalan kalau bukan mode demo."
        )

    channel = settings["telegram"]["channel"]
    if not channel:
        raise SystemExit("Isi telegram.channel di config/settings.yaml dulu.")

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH belum diisi di config/.env")

    session_path = settings["telegram"].get("session_name", "session/user")
    if not os.path.exists(f"{session_path}.session"):
        raise SystemExit(f"Session {session_path}.session tidak ada. Jalankan tools/login_telegram.py dulu.")

    if not mt5_client.connect():
        raise SystemExit(
            "Gagal connect ke MT5. Pastikan terminal MT5 sudah terbuka & login, "
            "dan package MetaTrader5 sudah ter-install."
        )
    demo = mt5_client.is_demo_account()
    if not demo:
        mt5_client.shutdown()
        raise SystemExit(
            "Akun yang login di terminal MT5 BUKAN akun demo. Dihentikan demi keamanan."
        )
    log.info("MT5 connected — akun DEMO terverifikasi.")

    broker_symbols = mt5_client.get_all_symbol_names()
    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )

    db = Database(DB_PATH)
    db.init_schema()
    ctx = Context(settings=settings, db=db, resolver=resolver, broker_symbols=broker_symbols)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start()

    log.info("Menyinkronkan daftar chat...")
    try:
        entity = await resolve_channel_entity(client, channel)
    except (ValueError, TypeError) as e:
        mt5_client.shutdown()
        raise SystemExit(f"Tidak bisa menemukan channel '{channel}'. Detail: {e}")

    log.info("Channel ditemukan: %s (id=%s)", getattr(entity, "title", channel), entity.id)
    notifier.send(f"🟢 Bot aktif — mendengarkan {getattr(entity, 'title', channel)} (mode: demo)")

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        await handle_new_message(ctx, str(channel), event.message)

    log.info("Mendengarkan channel: %s", channel)
    try:
        await client.run_until_disconnected()
    finally:
        mt5_client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
