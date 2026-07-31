"""Ukur ZONA WAKTU SERVER BROKER dari data, bukan menebaknya.

    .venv/bin/python tools/detect_server_timezone.py

Kenapa perlu: timestamp di file export MT5 (M5 maupun tick) adalah WAKTU
SERVER BROKER (mayoritas GMT+2/GMT+3), sedangkan timestamp pesan Telegram
di korpus sinyal adalah UTC ASLI. Kalau keduanya diperlakukan sama, SELURUH
backtest bergeser berjam-jam: sinyal dicocokkan ke candle yang salah dan
hasil TP/SL-nya tidak ada artinya.

Cara ukur: banyak sinyal channel menyebut "current price: X" pada saat
diposting. Itu jadi patokan kebenaran -- kita coba berbagai pergeseran
waktu, lalu pilih yang bikin harga di data kita PALING DEKAT dengan harga
yang channel sebut. Pergeseran yang benar akan terlihat sebagai minimum
yang TAJAM (error naik lagi di kiri-kanannya), bukan dataran landai.

Hasilnya ditaruh manual ke config/settings.yaml ->
backtest.server_utc_offset_hours. Jalankan ulang kalau ganti broker.
"""

import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.price_data import PriceSeries
from backtest.server_time import ServerClock
from src.parser.patterns import parse_entry_signal
from src.trading.symbols import SymbolResolver
from tools.run_backtest import DATA_DIR, PRICE_FILES, SIGNALS_PATH

CURRENT_PRICE = re.compile(r"current\s*(?:price)?\s*[:\-]?\s*([\d,]+\.?\d*)", re.I)
BROKER_SYMBOLS = ["XAUUSD+", "NAS100.r", "DJ30.r", "SP500.r"]


def collect_samples(series, resolver):
    """[(canonical, waktu_pesan_utc, harga_yang_disebut_channel)]"""
    samples = []
    with open(SIGNALS_PATH) as f:
        for line in f:
            row = json.loads(line)
            text = row["text"] or ""
            m = CURRENT_PRICE.search(text)
            if not m:
                continue
            signal = parse_entry_signal(text, message_id=row["message_id"])
            if signal is None:
                continue
            resolved = resolver.resolve(signal.symbol, BROKER_SYMBOLS)
            if not resolved.ok or resolved.canonical not in series:
                continue
            try:
                claimed = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if claimed <= 0:
                continue
            samples.append((resolved.canonical, datetime.fromisoformat(row["date_utc"]), claimed))
    return samples


def median_error_pct(series, samples, shift_hours):
    """Median |error| relatif (%) antara harga di data kita (pada waktu
    yang sudah digeser) vs harga yang channel sebut. Pakai persen supaya
    XAUUSD (~4000) dan US30 (~52000) bisa dibandingkan setara."""
    errors = []
    shift = timedelta(hours=shift_hours)
    for canonical, dt, claimed in samples:
        s = series[canonical]
        idx = s.index_at_or_after(dt + shift)
        if idx is None:
            continue
        candle = s.candles[idx]
        mid = (candle.high + candle.low) / 2
        errors.append(abs(mid - claimed) / claimed * 100)
    return statistics.median(errors) if errors else None


def median_error_for_clock(series, samples, clock):
    """Sama seperti median_error_pct tapi pakai ServerClock penuh, jadi
    kandidat yang paham DST bisa diadu langsung dengan offset tetap."""
    errors = []
    for canonical, dt, claimed in samples:
        s = series[canonical]
        # series di-load mentah (offset 0), jadi query digeser manual ke
        # waktu server menurut clock yang sedang diuji.
        idx = s.index_at_or_after(clock.to_server(dt).replace(tzinfo=timezone.utc))
        if idx is None:
            continue
        candle = s.candles[idx]
        mid = (candle.high + candle.low) / 2
        errors.append(abs(mid - claimed) / claimed * 100)
    return statistics.median(errors) if errors else None


def monthly_best_offsets(series, samples):
    """Offset terbaik DIUKUR ULANG TERPISAH tiap bulan -- kalau hasilnya
    berbeda antara musim dingin dan musim panas, berarti broker pakai DST
    dan offset tetap TIDAK akan pernah benar sepanjang tahun."""
    by_month = defaultdict(list)
    for sample in samples:
        by_month[sample[1].strftime("%Y-%m")].append(sample)

    rows = []
    for month in sorted(by_month):
        subset = by_month[month]
        if len(subset) < 8:
            continue
        scored = [
            (h * 0.25, median_error_pct(series, subset, h * 0.25))
            for h in range(0, 21)
        ]
        scored = [pair for pair in scored if pair[1] is not None]
        if not scored:  # semua di luar rentang data harga -> tidak terukur
            continue
        offset, err = min(scored, key=lambda pair: pair[1])
        rows.append((month, len(subset), offset, err))
    return rows


def main():
    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)
    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )

    print("Memuat data harga M5 APA ADANYA (tanpa koreksi zona waktu)...")
    series = {}
    for canonical, filename in PRICE_FILES.items():
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            print(f"  [!] {path} tidak ada, {canonical} dilewati")
            continue
        # offset 0 -- justru offset inilah yang mau diukur
        series[canonical] = PriceSeries.from_csv(path, server_utc_offset_hours=0.0)
        print(f"  {canonical}: {len(series[canonical])} candle")

    samples = collect_samples(series, resolver)
    print(f"\n{len(samples)} sinyal menyebut 'current price' -- dipakai sebagai patokan.\n")
    if not samples:
        print("Tidak ada sampel; tidak bisa mengukur.")
        return

    print("Median |error| harga (%) untuk tiap pergeseran waktu TETAP:")
    print("(pergeseran = berapa jam waktu pencarian digeser di data kita)\n")
    results = []
    for quarter in range(-16, 21):  # -4h s/d +5h, langkah 15 menit
        hours = quarter * 0.25
        err = median_error_pct(series, samples, hours)
        if err is not None:
            results.append((hours, err))

    best_hours, best_err = min(results, key=lambda r: r[1])
    worst_err = max(r[1] for r in results)
    for hours, err in results:
        bar = "#" * int(err / worst_err * 50)
        mark = "  <== terbaik utk offset TETAP" if hours == best_hours else ""
        print(f"  {hours:+6.2f}h  {err:7.4f}%  {bar}{mark}")

    # --- Apakah broker pakai DST? ---
    print("\n" + "=" * 66)
    print("OFFSET TERBAIK DIUKUR ULANG TERPISAH TIAP BULAN")
    print("(kalau musim dingin dan musim panas beda, berarti broker pakai DST")
    print(" dan offset TETAP tidak akan pernah benar sepanjang tahun)")
    print("=" * 66)
    monthly = monthly_best_offsets(series, samples)
    for month, n, offset, err in monthly:
        print(f"  {month}  n={n:4}  offset {offset:+5.2f}h  (err {err:.4f}%)")

    offsets_seen = {row[2] for row in monthly}
    dst_suspected = len(offsets_seen) > 1 and (max(offsets_seen) - min(offsets_seen)) >= 0.5

    # --- Adu kandidat konfigurasi lengkap ---
    print("\n" + "=" * 66)
    print("ADU KANDIDAT KONFIGURASI (makin kecil makin cocok)")
    print("=" * 66)
    candidates = [
        (f"offset tetap UTC{best_hours:+g}", ServerClock(fixed_offset_hours=best_hours),
         {"server_utc_offset_hours": best_hours}),
    ]
    try:
        from zoneinfo import ZoneInfo

        # basis UTC+2 dgn aturan DST Amerika (banyak broker begini: server
        # EET tapi ikut kalender New York) vs aturan DST Eropa
        candidates.append((
            "America/New_York +7h (basis UTC+2, DST Amerika)",
            ServerClock(tz=ZoneInfo("America/New_York"), extra_hours=7),
            {"server_timezone": "America/New_York", "server_timezone_extra_hours": 7},
        ))
        candidates.append((
            "Europe/Athens (basis UTC+2, DST Eropa)",
            ServerClock(tz=ZoneInfo("Europe/Athens")),
            {"server_timezone": "Europe/Athens"},
        ))
    except ImportError:
        print("  (zoneinfo/tzdata tidak tersedia -- kandidat DST dilewati)")

    scored = []
    for label, clock, config in candidates:
        err = median_error_for_clock(series, samples, clock)
        if err is not None:
            scored.append((err, label, config))
    scored.sort()
    for err, label, _ in scored:
        print(f"  {err:7.4f}%   {label}")

    print("\n" + "=" * 66)
    if dst_suspected:
        print("KESIMPULAN: broker ini PAKAI DST (offset musim dingin != musim panas).")
        print("Offset tetap TIDAK cukup -- pakai zona bernama.")
    else:
        print("KESIMPULAN: tidak terdeteksi DST; offset tetap sudah memadai.")
    if scored:
        _, best_label, best_config = scored[0]
        print(f"Paling cocok: {best_label}")
        print("\nSetel di config/settings.yaml:")
        print("  backtest:")
        for key, value in best_config.items():
            print(f"    {key}: {value}")


if __name__ == "__main__":
    main()
