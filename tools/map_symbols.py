"""Fase 3, dijalankan di mini PC Windows (butuh MT5 terinstal & login).

    python tools/map_symbols.py

Untuk setiap alias kanonik di config/settings.yaml -> symbols.aliases,
tool ini mengambil daftar simbol persis dari broker (mt5.symbols_get()),
menyarankan kandidat yang namanya cocok, dan minta kamu KONFIRMASI manual
satu per satu. Hasilnya ditulis ke symbols.broker_overrides di
settings.yaml.

Kenapa harus konfirmasi manual: kalau broker punya beberapa varian simbol
sekaligus (mis. XAUUSD, XAUUSD.raw, XAUUSDm untuk jenis akun berbeda),
menebak otomatis bisa membuat order jalan di instrumen/akun yang salah
tanpa ketahuan. Sekali dikonfirmasi, mapping ini permanen sampai kamu
ubah manual atau ganti broker.
"""

import os
import sys

import yaml

# Supaya "src" bisa diimpor walau script ini dijalankan langsung
# (python tools\map_symbols.py) dari luar folder project.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.trading.symbols import SymbolResolver  # noqa: E402

SETTINGS_PATH = "config/settings.yaml"


def load_settings():
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def save_settings(settings):
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(settings, f, sort_keys=False, allow_unicode=True)


def main():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit(
            "Modul MetaTrader5 tidak ditemukan. Tool ini harus dijalankan di "
            "mini PC Windows dengan `pip install MetaTrader5` dan terminal MT5 "
            "sudah login (lihat Fase 0 di plan)."
        )

    if not mt5.initialize():
        raise SystemExit(f"mt5.initialize() gagal: {mt5.last_error()}")

    settings = load_settings()
    aliases = settings["symbols"]["aliases"]
    overrides = settings["symbols"].get("broker_overrides") or {}

    broker_symbols = [s.name for s in mt5.symbols_get()]
    print(f"Ditemukan {len(broker_symbols)} simbol di broker.\n")

    resolver = SymbolResolver(aliases, overrides)

    for canonical in aliases:
        if canonical in overrides:
            print(f"[skip] {canonical} sudah punya override: {overrides[canonical]}")
            continue

        result = resolver.resolve(canonical, broker_symbols)
        if result.ok:
            print(f"[auto] {canonical} -> {result.matched} (cocok tunggal, tidak ambigu)")
            overrides[canonical] = result.matched
            continue

        suggestions = resolver.suggest(canonical, broker_symbols)
        if not suggestions:
            print(f"[!] {canonical}: tidak ada kandidat ditemukan di broker. Lewati (skip).")
            continue

        print(f"\n{canonical} — pilih simbol broker yang benar:")
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. {s}")
        print("  0. lewati (skip)")
        choice = input("  Pilihan: ").strip()
        if choice == "0" or choice == "":
            continue
        try:
            idx = int(choice) - 1
            overrides[canonical] = suggestions[idx]
        except (ValueError, IndexError):
            print("  Input tidak valid, dilewati.")

    settings["symbols"]["broker_overrides"] = overrides
    save_settings(settings)
    print(f"\nDisimpan ke {SETTINGS_PATH}: {overrides}")

    mt5.shutdown()


if __name__ == "__main__":
    main()
