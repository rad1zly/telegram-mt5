"""Uji kejujuran channel: tiap klaim "Hit Target / Hit SL, the price
reached X" dicek ke data harga M5 ASLI -- apakah harga BENERAN sampai
level itu?

    .venv/bin/python tools/verify_channel_claims.py
    .venv/bin/python tools/verify_channel_claims.py --days 90
    .venv/bin/python tools/verify_channel_claims.py --days 30 --show-all

Kenapa pakai LEVEL HARGA, bukan angka pip yang mereka sebut: angka pip
bisa dihitung dari titik mana saja (dan itulah cara channel menggelembungkan
statistik). Level harga absolut TIDAK bisa dikarang -- harga sungguhan
menyentuhnya atau tidak. Itu yang bikin uji ini falsifiable.

Sinyal induk tiap klaim dilacak lewat reply_to_msg_id (reply-chain
Telegram), lalu dicek apakah harga menyentuh level yang diklaim antara
waktu sinyal induk dan waktu klaim.

CATATAN: "meleset" tipis (di bawah ambang spread) dihitung terpisah dari
meleset sungguhan -- selisih sepersekian pip itu wajar (spread broker,
feed harga beda), bukan bukti channel bohong.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.price_data import PriceSeries
from backtest.server_time import ServerClock
from src.parser.patterns import parse_entry_signal
from src.trading.symbols import SymbolResolver
from tools.run_backtest import DATA_DIR, PRICE_FILES, SIGNALS_PATH

BROKER_SYMBOLS = ["XAUUSD+", "NAS100.r", "DJ30.r", "SP500.r"]

# Ambang "meleset tipis" per simbol (dalam satuan harga) -- kira-kira
# sebesar spread wajar instrumen itu. Meleset di bawah ini TIDAK dihitung
# sebagai klaim meleset, karena beda feed harga/spread antar broker saja
# sudah bisa sebesar itu.
NEAR_MISS_TOLERANCE = {"XAUUSD": 1.0, "NAS100": 10.0, "US30": 10.0, "SP500": 2.0}

# Urutan penting: pola spesifik dulu, supaya "reached our first target at X"
# tidak keburu ketangkap pola generik "reached X".
LEVEL_PATTERNS = [
    r"reached (?:our|the) (?:first |second |third |final )?target (?:at|of) ([\d,]+\.?\d*)",
    r"(?:dropped|fell|declined|moved down|rose|climbed|moved up)(?:\s+\w+){0,3}\s+to ([\d,]+\.?\d*)",
    r"reached the ([\d,]+\.?\d*)(?:\s+\w+){0,2}\s+level",
    r"reached ([\d,]+\.?\d*)",
    r"touch(?:ed)? (?:the )?([\d,]+\.?\d*)",
]

HIT_TARGET = re.compile(r"hit\s+(?:the\s+)?(?:target|profit)", re.I)
HIT_SL = re.compile(r"hit\s+(?:the\s+)?(?:sl|stop[\s-]?loss)", re.I)
PIP_CLAIM = re.compile(r"([+-]?\s*\d+(?:\.\d+)?)\s*pip", re.I)


def extract_level(text):
    for pattern in LEVEL_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def extract_pips(text):
    m = PIP_CLAIM.search(text)
    return float(m.group(1).replace(" ", "")) if m else None


def resolve_parent_signal(reply_to, rows_by_id, signal_by_id, max_depth=20):
    """Naik reply-chain sampai ketemu pesan yang memang entry signal."""
    seen = set()
    current = reply_to
    for _ in range(max_depth):
        if current is None or current in seen:
            return None
        seen.add(current)
        if current in signal_by_id:
            return signal_by_id[current]
        row = rows_by_id.get(current)
        if row is None:
            return None
        current = row.get("reply_to_msg_id")
    return None


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30, help="Berapa hari ke belakang diperiksa (default 30).")
    p.add_argument("--since", help="Tanggal mulai eksplisit (YYYY-MM-DD), mis. 2026-01-01. Menimpa --days.")
    p.add_argument("--show-all", action="store_true", help="Tampilkan semua klaim, bukan cuma yang meleset.")
    return p.parse_args()


def main():
    args = parse_args()
    with open("config/settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )
    price_offsets = settings["guards"].get("broker_price_offset_overrides") or {}
    clock = ServerClock.from_config(settings.get("backtest"))

    print(f"Memuat data harga M5... (server broker {clock.describe()}, dikoreksi ke UTC asli)")
    series = {}
    for canonical, filename in PRICE_FILES.items():
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            continue
        series[canonical] = PriceSeries.from_csv(path, clock=clock)

    with open(SIGNALS_PATH, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    rows_by_id = {r["message_id"]: r for r in rows}

    # Parse SEMUA entry signal (bukan cuma dalam window) supaya klaim di
    # awal window tetap bisa menemukan induknya yang lebih tua.
    signal_by_id = {}
    for r in rows:
        signal = parse_entry_signal(r["text"] or "", message_id=r["message_id"])
        if signal is not None:
            signal_by_id[r["message_id"]] = (signal, r)

    latest = max(datetime.fromisoformat(r["date_utc"]) for r in rows)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=latest.tzinfo)
    else:
        cutoff = latest - timedelta(days=args.days)
    recent = [r for r in rows if datetime.fromisoformat(r["date_utc"]) >= cutoff]
    window_label = f"sejak {args.since}" if args.since else f"{args.days} HARI TERAKHIR"
    print(f"Window: {cutoff.date()} s/d {latest.date()} ({len(recent)} pesan)\n")

    results = []
    for r in recent:
        text = r["text"] or ""
        is_target, is_sl = bool(HIT_TARGET.search(text)), bool(HIT_SL.search(text))
        if not (is_target or is_sl):
            continue

        rec = {
            "msg_id": r["message_id"],
            "time": datetime.fromisoformat(r["date_utc"]),
            "kind": "TARGET" if is_target else "SL",
            "level": extract_level(text),
            "pips": extract_pips(text),
            "headline": text.split("\n")[0][:44],
            "symbol": "?",
            "miss": None,
        }

        parent = resolve_parent_signal(r.get("reply_to_msg_id"), rows_by_id, signal_by_id)
        if parent is None:
            rec["verdict"], rec["detail"] = "TAK_TERLACAK", "sinyal induk tidak ketemu lewat reply-chain"
            results.append(rec)
            continue

        signal, parent_row = parent
        resolved = resolver.resolve(signal.symbol, BROKER_SYMBOLS)
        if not resolved.ok or resolved.canonical not in series:
            rec["verdict"], rec["detail"] = "TANPA_DATA", f"{signal.symbol} tidak ada data harganya"
            results.append(rec)
            continue

        canonical = resolved.canonical
        rec["symbol"] = canonical
        if rec["level"] is None:
            rec["verdict"], rec["detail"] = "TANPA_LEVEL", "teks tidak menyebut level harga yang bisa dicek"
            results.append(rec)
            continue

        level_broker = rec["level"] + price_offsets.get(canonical, 0.0)
        s = series[canonical]
        i0 = s.index_at_or_after(datetime.fromisoformat(parent_row["date_utc"]))
        i1 = s.index_at_or_after(rec["time"])
        if i0 is None:
            rec["verdict"], rec["detail"] = "DILUAR_DATA", "waktu sinyal di luar rentang data harga"
            results.append(rec)
            continue
        if i1 is None:
            i1 = len(s) - 1
        i1 = max(i1, min(i0 + 1, len(s) - 1))

        window = s.candles[i0:i1 + 1]
        lo = min(c.low for c in window)
        hi = max(c.high for c in window)

        if lo <= level_broker <= hi:
            rec["verdict"] = "TERBUKTI"
            rec["detail"] = f"harga menyentuh {level_broker:g} (range {lo:g}-{hi:g})"
        else:
            miss = (level_broker - hi) if level_broker > hi else (lo - level_broker)
            rec["miss"] = miss
            tolerance = NEAR_MISS_TOLERANCE.get(canonical, 0.0)
            rec["verdict"] = "MELESET_TIPIS" if miss <= tolerance else "MELESET"
            rec["detail"] = f"tidak sampai {level_broker:g}; range {lo:g}-{hi:g} (selisih {miss:g})"
        results.append(rec)

    print("=" * 74)
    print(f"VERIFIKASI KLAIM CHANNEL -- {window_label.upper()}")
    print("=" * 74)
    counts = Counter(r["verdict"] for r in results)
    print(f"Total klaim 'Hit Target/SL' : {len(results)}")
    for verdict, count in counts.most_common():
        print(f"  {verdict:14} : {count}")

    checkable = [r for r in results if r["verdict"] in ("TERBUKTI", "MELESET_TIPIS", "MELESET")]
    if checkable:
        proven = sum(1 for r in checkable if r["verdict"] == "TERBUKTI")
        near = sum(1 for r in checkable if r["verdict"] == "MELESET_TIPIS")
        real = sum(1 for r in checkable if r["verdict"] == "MELESET")
        n = len(checkable)
        print(f"\nDari {n} klaim yang bisa dicek ke harga sungguhan:")
        print(f"  terbukti persis      : {proven:3} ({proven/n*100:.1f}%)")
        print(f"  meleset tipis        : {near:3} ({near/n*100:.1f}%)  <- selisih spread, wajar")
        print(f"  MELESET sungguhan    : {real:3} ({real/n*100:.1f}%)")
        print(f"  => akurat (persis + tipis): {(proven+near)/n*100:.1f}%")

        for kind in ("TARGET", "SL"):
            sub = [r for r in checkable if r["kind"] == kind]
            if sub:
                ok = sum(1 for r in sub if r["verdict"] != "MELESET")
                print(f"     - klaim {kind:6}: {ok}/{len(sub)} akurat ({ok/len(sub)*100:.1f}%)")

    shown = results if args.show_all else [r for r in results if r["verdict"] == "MELESET"]
    if shown:
        print("\n" + "=" * 74)
        print("SEMUA KLAIM" if args.show_all else "KLAIM YANG MELESET SUNGGUHAN")
        print("=" * 74)
        for r in sorted(shown, key=lambda x: x["time"]):
            print(f"[{r['verdict']:13}] #{r['msg_id']} {r['time']:%Y-%m-%d %H:%M} "
                  f"{r['symbol']:7} {r['kind']:6} klaim={r['pips']} pip lvl={r['level']}")
            print(f"    {r['headline']}")
            print(f"    -> {r['detail']}")


if __name__ == "__main__":
    main()
