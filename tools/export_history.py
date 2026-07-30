"""Export histori harga (candle M5) dari MT5 untuk keperluan backtest.

    .venv\\Scripts\\python.exe tools\\export_history.py

Jalankan di mini PC (butuh MT5 terinstal & terminal login). Mengambil
candle M5 sejauh mungkin (broker akan otomatis membatasi kalau histori
tidak selengkap itu) untuk 4 instrumen dengan volume sinyal terbanyak,
plus snapshot symbol_info (point/tick_value/volume_step dst) supaya
perhitungan lot saat backtest konsisten dengan live.

Output: backtest/data/<SYMBOL>_M5.csv dan backtest/data/symbol_info.yaml
File-file ini perlu dikirim balik (cloud/USB/dll) untuk dipakai
membangun & menjalankan backtest engine.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from src.trading import mt5_client

# 4 instrumen dengan volume sinyal terbanyak (~93% dari total) — lihat
# ringkasan analisis korpus. Tambah simbol lain di sini kalau nanti mau
# perluas cakupan backtest.
SYMBOLS = ["XAUUSD+", "NAS100.r", "DJ30.r", "SP500.r"]

OUTPUT_DIR = "backtest/data"
YEARS_BACK = 2  # diminta broker akan otomatis dipotong kalau histori tidak selengkap itu


def export_candles(mt5, symbol: str, start: datetime, end: datetime) -> int:
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, start, end)
    if rates is None or len(rates) == 0:
        print(f"[!] Tidak ada data candle untuk {symbol}: {mt5.last_error()}")
        return 0

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_name = symbol.replace("/", "_").replace("+", "plus")
    path = os.path.join(OUTPUT_DIR, f"{safe_name}_M5.csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_utc", "open", "high", "low", "close", "tick_volume"])
        for r in rates:
            writer.writerow([
                datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(),
                r["open"], r["high"], r["low"], r["close"], r["tick_volume"],
            ])
    print(f"{symbol}: {len(rates)} candle -> {path}")
    return len(rates)


def export_symbol_info(mt5, symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"[!] symbol_info kosong untuk {symbol}")
        return None
    return {
        "symbol": symbol,
        "point": info.point,
        "digits": info.digits,
        "trade_tick_size": info.trade_tick_size,
        "trade_tick_value": info.trade_tick_value,
        "volume_step": info.volume_step,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "trade_stops_level": info.trade_stops_level,
    }


def main():
    if not mt5_client.connect():
        raise SystemExit(
            "Gagal connect ke MT5. Pastikan terminal MT5 sudah terbuka & login."
        )

    import MetaTrader5 as mt5

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * YEARS_BACK)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    info_snapshot = []

    for symbol in SYMBOLS:
        print(f"\n=== {symbol} ===")
        export_candles(mt5, symbol, start, end)
        info = export_symbol_info(mt5, symbol)
        if info:
            info_snapshot.append(info)

    with open(os.path.join(OUTPUT_DIR, "symbol_info.yaml"), "w") as f:
        yaml.safe_dump(info_snapshot, f, sort_keys=False)

    print(f"\nSelesai. Semua file ada di folder: {OUTPUT_DIR}/")
    print("Kirim folder ini (CSV + symbol_info.yaml) untuk dipakai membangun backtest.")

    mt5_client.shutdown()


if __name__ == "__main__":
    main()
