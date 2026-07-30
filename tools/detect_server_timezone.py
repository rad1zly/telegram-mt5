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
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.price_data import PriceSeries
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

    print("Median |error| harga (%) untuk tiap pergeseran waktu:")
    print("(pergeseran = berapa jam waktu pencarian digeser di data kita)\n")
    results = []
    for quarter in range(-16, 21):  # -4h s/d +5h, langkah 15 menit
        hours = quarter * 0.25
        err = median_error_pct(series, samples, hours)
        if err is None:
            continue
        results.append((hours, err))

    best_hours, best_err = min(results, key=lambda r: r[1])
    worst_err = max(r[1] for r in results)
    for hours, err in results:
        bar = "#" * int(err / worst_err * 50)
        mark = "  <== PALING COCOK" if hours == best_hours else ""
        print(f"  {hours:+6.2f}h  {err:7.4f}%  {bar}{mark}")

    print(f"\nHASIL: server broker = UTC{best_hours:+g} (median error {best_err:.4f}%)")
    current = (settings.get("backtest") or {}).get("server_utc_offset_hours", 0.0)
    if abs(current - best_hours) < 0.01:
        print(f"Config sudah benar (backtest.server_utc_offset_hours: {current:g}).")
    else:
        print(f"Config saat ini {current:g} -- UBAH ke {best_hours:g} di config/settings.yaml:")
        print(f"  backtest:\n    server_utc_offset_hours: {best_hours:g}")


if __name__ == "__main__":
    main()
