"""Uji coba manual: parse 1 signal, lalu KIRIM order sungguhan ke akun
MT5 yang sedang login di komputer ini.

    .venv\\Scripts\\python.exe tools\\execute_test_signal.py

Prasyarat:
  - Package MetaTrader5 sudah di-install (pip install MetaTrader5)
  - Terminal MT5 sudah dibuka dan LOGIN ke akun DEMO
  - config/settings.yaml -> mode: demo (script menolak jalan kalau bukan)

Ini BUKAN pipeline penuh — belum ada guard spread/deviasi harga/max-trade-
harian (itu menyusul saat integrasi live 24/7). Tujuannya cuma verifikasi
alur parser -> resolve simbol -> hitung lot -> order MT5 benar-benar
nyambung, dengan konfirmasi manual sebelum order beneran terkirim.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

from src.parser.patterns import parse_entry_signal
from src.trading import mt5_client
from src.trading.executor import execute_signal
from src.trading.symbols import SymbolResolver

load_dotenv(dotenv_path="config/.env")

SAMPLE_SIGNALS = {
    "1": "US30\n\nSell now below 48500\n\nTarget 48420, 48300 , 48100\nSl.: 48560 \n\nRisk 1%",
    "2": "GOLD \n\nsell below 4344 - 4345\n\ntp.: 4333, 4323\nsl.: 4348\n\nrisk 1%",
    "3": "USDJPY \n\nSell Below 156.600\n\ntp.: 156.00, 155.34, 154.45\nsl.: 156.80\n\nrisk 1%",
}


def load_settings():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def main():
    settings = load_settings()
    if settings.get("mode") != "demo":
        raise SystemExit(
            f"config/settings.yaml -> mode = '{settings.get('mode')}', bukan 'demo'. "
            "Script test ini menolak jalan kalau bukan mode demo."
        )

    print("Pilih contoh signal untuk dites:")
    for key, text in SAMPLE_SIGNALS.items():
        symbol = text.splitlines()[0].strip()
        print(f"  {key}. {symbol}")
    print("  0. Tempel teks signal sendiri")
    choice = input("Pilihan: ").strip()

    if choice in SAMPLE_SIGNALS:
        text = SAMPLE_SIGNALS[choice]
    else:
        print("Tempel teks signal, lalu baris kosong untuk selesai:")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        text = "\n".join(lines)

    signal = parse_entry_signal(text, message_id=0)
    if signal is None:
        raise SystemExit("Parser gagal mengenali teks ini sebagai signal. Cek formatnya.")

    entry_display = signal.entry if signal.entry is not None else signal.entry_range
    print(
        f"\nTerparsing: {signal.action} {signal.symbol}  entry={entry_display}  "
        f"sl={signal.sl}  tp={signal.tp}\n"
    )

    if not mt5_client.connect():
        raise SystemExit(
            "Gagal connect ke MT5. Pastikan terminal MT5 sudah terbuka dan login, "
            "dan package MetaTrader5 sudah ter-install di venv ini."
        )

    demo = mt5_client.is_demo_account()
    if demo is None:
        mt5_client.shutdown()
        raise SystemExit("Tidak bisa membaca account_info dari MT5.")
    if not demo:
        mt5_client.shutdown()
        raise SystemExit(
            "Akun yang sedang login di terminal MT5 BUKAN akun demo. "
            "Dihentikan demi keamanan — script ini tidak akan kirim order ke akun live."
        )

    broker_symbols = mt5_client.get_all_symbol_names()
    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )

    confirm = input("Kirim order sungguhan ke akun DEMO ini sekarang? (ketik 'ya' untuk lanjut): ").strip().lower()
    if confirm != "ya":
        print("Dibatalkan.")
        mt5_client.shutdown()
        return

    result = execute_signal(
        signal=signal,
        resolver=resolver,
        broker_symbols=broker_symbols,
        risk_usd=settings["risk"]["usd_per_trade"],
        max_lot_cap=settings["risk"]["max_lot_cap"],
    )

    print(f"\n{'BERHASIL' if result.success else 'GAGAL'}: {result.detail}")
    if result.ticket:
        print(f"Ticket: {result.ticket}")

    mt5_client.shutdown()


if __name__ == "__main__":
    main()
