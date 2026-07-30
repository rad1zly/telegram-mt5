"""Cetak symbol_info 4 instrumen utama — buat backtest butuh tick_value
yang akurat (tidak ada di data CSV harga, cuma ada dari MT5 langsung).

    .venv\\Scripts\\python.exe tools\\print_symbol_info.py

Copy-paste hasil di layar dan kirim balik — tidak perlu export file apa-apa.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading import mt5_client

SYMBOLS = ["XAUUSD+", "NAS100.r", "DJ30.r", "SP500.r"]


def main():
    if not mt5_client.connect():
        raise SystemExit("Gagal connect ke MT5. Pastikan terminal MT5 sudah terbuka & login.")

    for symbol in SYMBOLS:
        info = mt5_client.get_symbol_info(symbol)
        if info is None:
            print(f"{symbol}: symbol_info KOSONG")
            continue
        print(f"--- {symbol} ---")
        print(f"point: {info.point}")
        print(f"digits: {info.digits}")
        print(f"trade_tick_size: {info.trade_tick_size}")
        print(f"trade_tick_value: {info.trade_tick_value}")
        print(f"volume_step: {info.volume_step}")
        print(f"volume_min: {info.volume_min}")
        print(f"volume_max: {info.volume_max}")
        print(f"trade_stops_level: {info.trade_stops_level}")
        print()

    mt5_client.shutdown()


if __name__ == "__main__":
    main()
