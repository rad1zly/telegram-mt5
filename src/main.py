"""Pipeline penuh: listener Telegram -> parser -> eksekusi MT5 -> notifikasi.

    .venv\\Scripts\\python.exe src\\main.py

WAJIB config/settings.yaml -> mode: demo, dan akun yang login di terminal
MT5 WAJIB benar-benar demo (dicek dari account_info, bukan cuma config) —
kalau tidak, proses berhenti sebelum sempat listen sama sekali.

Alur per pesan baru (dan pesan yang di-EDIT — channel ini sering edit
pesan lama untuk menambah update, bukan cuma kirim pesan baru):
  1. Dedup (SQLite, unique per message_id) — pesan yang sama diabaikan.
     Untuk pesan yang di-edit: diproses ulang HANYA kalau teksnya benar-
     benar berubah, dan kalau message_id itu SUDAH pernah dieksekusi jadi
     posisi, tidak dieksekusi ulang otomatis (dianggap koreksi teks, bukan
     sinyal baru) — cuma notifikasi ke user.
  2. Coba parse sebagai entry signal (regex, lalu fallback MiniMax kalau
     MINIMAX_API_KEY ada). Kalau berhasil -> resolve simbol, hitung lot
     dari risiko tetap, kirim order, simpan posisi, notifikasi.
  3. Kalau bukan entry, coba parse sebagai follow-up "Live Update" (regex,
     lalu fallback MiniMax). Sebelum menerapkan aksi, posisi yang match
     di-verifikasi ULANG ke broker (bukan cuma percaya status 'open' di
     DB lokal — bisa stale kalau sudah kena TP/SL). Kalau ada kinds yang
     cocok (move_sl_be / partial_close_tp1) DAN diaktifkan di config,
     diterapkan; partial close dibulatkan ke volume_step broker (bukan
     dibulatkan generik), dan kalau sisanya di bawah volume_min, tutup
     penuh saja. close_all tidak pernah dieksekusi otomatis, cuma
     notifikasi (lihat plan).
  4. Bukan keduanya -> diabaikan (banyak chatter non-signal di channel).

Guard yang AKTIF: dedup, SL wajib ada, simbol harus ke-resolve jelas, lot
harus lolos volume_min, max_trades_per_day, posisi diverifikasi ulang ke
broker sebelum follow-up diterapkan. Guard yang BELUM ada (spread,
daily_loss_cap) — lihat plan Fase 3/4, menyusul.
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
from src.parser.llm_fallback import classify_message_with_llm, parse_followup_with_llm, parse_signal_with_llm
from src.parser.patterns import parse_entry_signal
from src.parser.schema import FollowUp, Signal
from src.store.db import Database
from src.tg import notifier
from src.tg.listener import resolve_channel_entity
from src.trading import mt5_client
from src.trading.executor import execute_signal
from src.trading.risk import calculate_partial_close_volume
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


def today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def today_start_iso() -> str:
    return today_start().isoformat()


def _message_originally_from_before_today(msg) -> bool:
    """True kalau pesan ini ASLINYA dikirim SEBELUM hari kalender berjalan
    (UTC) — dipakai di handle_edited_message untuk mencegah event EDIT pada
    pesan LAMA (dikirim hari-hari sebelumnya) diproses seolah sinyal baru.
    msg.date Telethon = waktu KIRIM ASLI pesan, BUKAN waktu edit — channel
    ini sering menambah catatan ke sinyal lama yang sudah closed/SL lewat
    edit, dan teks aslinya (entry/SL/TP) tetap ada di situ, jadi kalau
    diproses ulang lewat classify_and_act bisa salah dieksekusi sebagai
    posisi BARU padahal cuma catatan ke sinyal mati."""
    if msg.date is None:
        return False
    return msg.date.astimezone(timezone.utc) < today_start()


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
            min_sl_distance_overrides=ctx.settings["guards"].get("min_sl_distance_overrides"),
            price_offset_overrides=ctx.settings["guards"].get("broker_price_offset_overrides"),
        ),
    )

    if not result.success:
        notifier.send(f"⚠️ Signal #{msg.id} ({signal.symbol}) GAGAL dieksekusi:\n{result.detail}")
        return

    ctx.db.insert_position({
        "signal_id": msg.id,
        "ticket": result.ticket,
        "symbol": signal.symbol,
        "direction": signal.action,
        "lot": result.lot,
        "open_price": result.price,
        "sl": signal.sl,
        "tp": signal.tp[-1] if signal.tp else None,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
    })
    notifier.send(f"✅ Signal #{msg.id} dieksekusi:\n{result.detail}")


async def _find_live_position(ctx: Context, loop, symbol: str):
    """Ambil posisi terbuka TERBARU untuk simbol ini yang MASIH BENAR-BENAR
    ADA di broker. Status 'open' di DB lokal bisa stale (posisi sudah kena
    TP/SL/ditutup manual tanpa kita tahu) — iterasi dari yang terbaru,
    sinkronkan ke 'closed' kalau ternyata sudah tidak ada, lanjut ke
    kandidat berikutnya."""
    candidates = ctx.db.get_open_positions_by_symbol(symbol)
    for candidate in candidates:
        broker_position = await loop.run_in_executor(None, mt5_client.get_position, candidate["ticket"])
        if broker_position is None:
            ctx.db.close_position(candidate["id"], datetime.now(timezone.utc).isoformat())
            log.info(
                "Posisi #%s (%s) sudah tidak ada di broker — status lokal disinkronkan ke closed",
                candidate["ticket"], symbol,
            )
            continue
        return candidate
    return None


async def handle_followup(ctx: Context, followup, msg) -> None:
    if not followup.kinds:
        preview = followup.raw_text[:200].replace("\n", " | ")
        notifier.send(f"ℹ️ Update #{msg.id} ({followup.symbol or '?'}): {preview}")
        return

    if followup.symbol is None:
        notifier.send(f"⚠️ Follow-up #{msg.id} tidak jelas simbolnya — dilewati, cek manual.")
        return

    loop = asyncio.get_event_loop()

    position = await _find_live_position(ctx, loop, followup.symbol)
    if position is None:
        notifier.send(
            f"⚠️ Follow-up #{msg.id} ({followup.symbol}) — tidak ada posisi terbuka yang cocok "
            f"(atau semua sudah closed di broker), dilewati."
        )
        return

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
            info = await loop.run_in_executor(None, mt5_client.get_symbol_info, broker_symbol)
            if info is None:
                notifier.send(f"⚠️ Tidak bisa ambil symbol_info untuk partial close {broker_symbol}")
            else:
                pc = calculate_partial_close_volume(
                    position_lot=position["lot"],
                    percent=ctx.settings["followup"]["partial_close_percent"],
                    volume_step=info.volume_step,
                    volume_min=info.volume_min,
                )
                if not pc.ok:
                    notifier.send(f"⚠️ Partial close #{position['ticket']} ditolak: {pc.error}")
                else:
                    close_volume = pc.volume if pc.action == "partial" else position["lot"]
                    result = await loop.run_in_executor(
                        None,
                        lambda: mt5_client.partial_close(position["ticket"], broker_symbol, close_volume),
                    )
                    if result.success:
                        ctx.db.mark_tp1_hit(position["id"])
                        if pc.action == "full":
                            ctx.db.close_position(position["id"], datetime.now(timezone.utc).isoformat())
                            notifier.send(
                                f"✅ Posisi #{position['ticket']} ({followup.symbol}) ditutup PENUH "
                                f"({close_volume} lot) — sisa setelah partial di bawah volume_min broker"
                            )
                        else:
                            notifier.send(f"✅ Partial close {close_volume} lot #{position['ticket']} ({followup.symbol})")
                            # SL+ otomatis: begitu TP1/partial-close berhasil, SL dipindah
                            # ke breakeven+buffer -- aturan risk management sendiri,
                            # TIDAK bergantung apakah channel juga bilang "pindah SL".
                            buffer = ctx.settings["followup"].get("sl_plus_buffer_overrides", {}).get(
                                resolved.canonical, 0.0
                            )
                            if buffer > 0 and position["open_price"] is not None and position["direction"]:
                                sl_plus = (
                                    position["open_price"] + buffer
                                    if position["direction"] == "BUY"
                                    else position["open_price"] - buffer
                                )
                                sl_result = await loop.run_in_executor(
                                    None,
                                    lambda: mt5_client.modify_sl_tp(position["ticket"], broker_symbol, sl=sl_plus),
                                )
                                if sl_result.success:
                                    ctx.db.mark_be_moved(position["id"])
                                    notifier.send(
                                        f"✅ SL posisi #{position['ticket']} ({followup.symbol}) dipindah ke "
                                        f"SL+ ({sl_plus}) — kunci profit setelah partial close"
                                    )
                                else:
                                    notifier.send(f"⚠️ Gagal pindah SL ke SL+ #{position['ticket']}: {sl_result.error}")
                    else:
                        notifier.send(f"⚠️ Gagal partial close #{position['ticket']}: {result.error}")

    if "close_all" in followup.kinds:
        if ctx.settings["followup"]["close_all"]:
            # Ambil volume LANGSUNG dari broker (bukan position["lot"] lokal,
            # yang bisa stale kalau sudah ada partial close sebelumnya).
            broker_position = await loop.run_in_executor(None, mt5_client.get_position, position["ticket"])
            if broker_position is None:
                notifier.send(
                    f"ℹ️ Close-all #{position['ticket']} ({followup.symbol}): posisi sudah tidak ada di broker "
                    f"(mungkin sudah closed lebih dulu)."
                )
            else:
                result = await loop.run_in_executor(
                    None,
                    lambda: mt5_client.partial_close(position["ticket"], broker_symbol, broker_position.volume),
                )
                if result.success:
                    ctx.db.close_position(position["id"], datetime.now(timezone.utc).isoformat())
                    notifier.send(f"✅ Posisi #{position['ticket']} ({followup.symbol}) ditutup PENUH (close_all)")
                else:
                    notifier.send(f"⚠️ Gagal close_all #{position['ticket']}: {result.error}")
        else:
            notifier.send(
                f"ℹ️ Channel minta CLOSE ALL untuk {followup.symbol} (#{position['ticket']}) — "
                f"TIDAK dieksekusi otomatis (close_all off di config). Cek manual kalau perlu."
            )


async def classify_and_act(ctx: Context, text: str, msg) -> None:
    # MODE TRIAL "LLM-first" (parser.llm_first: true di config): SATU
    # panggilan LLM per pesan menggantikan regex sebagai pengambil
    # keputusan UTAMA (bukan cuma fallback saat regex gagal) -- lihat
    # classify_message_with_llm. Untuk REVERT ke perilaku regex-first
    # (default sebelumnya), tinggal set parser.llm_first: false di
    # config/settings.yaml, tidak perlu ubah kode.
    if ctx.settings.get("parser", {}).get("llm_first") and llm_available():
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: classify_message_with_llm(text, message_id=msg.id, reply_to_msg_id=msg.reply_to_msg_id),
        )
        if isinstance(result, Signal):
            await handle_entry_signal(ctx, result, msg)
            return
        if isinstance(result, FollowUp):
            await handle_followup(ctx, result, msg)
            return
        log.info("Pesan #%s (LLM-first): tidak ada tool dipanggil — diabaikan", msg.id)
        return

    # --- Path regex-first (default sebelumnya / jalur revert) ---
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


def _message_row(channel_label: str, msg) -> dict:
    return {
        "message_id": msg.id,
        "channel": channel_label,
        "date_utc": msg.date.astimezone(timezone.utc).isoformat(),
        "text": msg.raw_text or "",
        "reply_to_msg_id": msg.reply_to_msg_id,
        "raw_json": json.dumps(msg.to_dict(), default=str),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


async def handle_new_message(ctx: Context, channel_label: str, msg) -> None:
    text = msg.raw_text or ""
    if not ctx.db.insert_message(_message_row(channel_label, msg)):
        log.debug("Pesan duplikat #%s diabaikan", msg.id)
        return
    await classify_and_act(ctx, text, msg)


async def handle_edited_message(ctx: Context, channel_label: str, msg) -> None:
    """Channel ini kadang meng-edit pesan lama untuk menambahkan update
    (mis. entry signal di-edit jadi berisi 'TP1 hit') — kalau kita cuma
    dengar NewMessage, semua edit ini terlewat sama sekali.

    Guard penting #1: kalau message_id ini SUDAH pernah dieksekusi jadi
    posisi, jangan eksekusi ulang otomatis walau teks barunya juga
    terlihat seperti entry baru — bisa jadi cuma koreksi typo pada
    signal yang sama, bukan sinyal baru. Serahkan ke manusia.

    Guard penting #2: kalau pesan ini ASLINYA dikirim SEBELUM hari ini
    (bukan baru saja) — entah karena bot belum pernah mencatatnya sama
    sekali (previous_text is None, mis. baru online lagi setelah offline
    beberapa hari) atau sudah tercatat tapi baru sekarang berubah jadi
    terlihat seperti entry — JANGAN dieksekusi otomatis. Ini nutup celah
    nyata: event MessageEdited bisa datang untuk pesan lama yang sudah
    closed/SL, dan teks entry aslinya masih match pola sinyal, jadi tanpa
    guard ini bot bisa "tiba-tiba" buka posisi baru padahal tidak ada
    sinyal baru sama sekali yang terlihat di channel.
    """
    text = msg.raw_text or ""
    previous_text = ctx.db.get_message_text(channel_label, msg.id)

    if previous_text is None:
        # Belum pernah tercatat (mis. bot baru start setelah edit terjadi,
        # atau ini pesan lama yang baru sekarang di-edit -- lihat guard #2
        # di bawah) -> catat dulu.
        if not ctx.db.insert_message(_message_row(channel_label, msg)):
            return
    elif previous_text == text:
        log.debug("Pesan #%s di-edit tapi teks tidak berubah — diabaikan", msg.id)
        return
    else:
        log.info("Pesan #%s di-edit, teks berubah — diproses ulang", msg.id)
        ctx.db.update_message_text(channel_label, msg.id, text)

        existing_position = ctx.db.get_position_by_signal_id(msg.id)
        if existing_position is not None:
            preview = text[:300].replace("\n", " | ")
            notifier.send(
                f"⚠️ Signal #{msg.id} ({existing_position['symbol']}) DIEDIT channel SETELAH "
                f"dieksekusi (ticket #{existing_position['ticket']}).\nTeks baru: {preview}\n"
                f"Tidak dieksekusi ulang otomatis — cek manual kalau perlu penyesuaian posisi."
            )
            return

    if _message_originally_from_before_today(msg):
        original_date = msg.date.astimezone(timezone.utc).date()
        preview = text[:300].replace("\n", " | ")
        notifier.send(
            f"⚠️ Pesan #{msg.id} (asli diposting {original_date}, BUKAN hari ini) baru terlihat "
            f"lewat event EDIT — TIDAK diproses otomatis sebagai sinyal baru (kemungkinan besar "
            f"cuma tambahan catatan ke sinyal LAMA, mis. hasil TP/SL). Cek manual kalau memang "
            f"perlu ditindaklanjuti.\nTeks: {preview}"
        )
        return

    await classify_and_act(ctx, text, msg)


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

    @client.on(events.MessageEdited(chats=entity))
    async def edit_handler(event):
        await handle_edited_message(ctx, str(channel), event.message)

    log.info("Mendengarkan channel: %s", channel)
    try:
        await client.run_until_disconnected()
    finally:
        mt5_client.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
