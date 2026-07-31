"""Jalankan backtest PRESISI TICK: replay tests/fixtures/signals.jsonl
terhadap data TICK asli (bukan candle M5) di backtest/data/ticks/.

    .venv/bin/python tools/run_backtest_ticks.py
    .venv/bin/python tools/run_backtest_ticks.py --tp-mode channel
    .venv/bin/python tools/run_backtest_ticks.py --source llm
    .venv/bin/python tools/run_backtest_ticks.py --tick-dir "D:/exports" --us30-ticks "DJ30_2025.csv"

CARA SIAPKAN DATA TICK (di MT5, di PC yang sama dengan data ini):
  1. Tools -> History Center (atau Ctrl+U)
  2. Pilih simbol (mis. DJ30.r), klik "Export Ticks" (bukan "Export Bars")
  3. Pilih rentang tanggal yang sama dengan data M5 yang sudah ada
     (cek backtest/data/<symbol>_M5.csv baris pertama utk tanggal mulainya)
  4a. Kalau RAM PC cukup besar (file per simbol muat nyaman di RAM):
      simpan sebagai backtest/data/ticks/<broker_symbol>_ticks.csv, ATAU
      simpan di mana saja dan arahkan lewat --tick-dir / --<symbol>-ticks
      (lihat contoh di atas) -- nama file BEBAS.
  4b. Kalau RAM PC TERBATAS (data tick puluhan GB, RAM cuma beberapa GB):
      convert dulu SEKALI pakai tools/prepare_tick_binary.py (proses
      bertahap, hemat memori), lalu arahkan --<symbol>-ticks ke PREFIX
      hasil convert itu (TANPA akhiran .csv) -- otomatis dibaca lewat
      memory-map (numpy.memmap), OS cuma nge-load bagian yang benar-benar
      dipakai ke RAM, bukan seluruh file. Lihat docstring tools/prepare_tick_binary.py.

Kalau file tick utk suatu simbol tidak ada, simbol itu OTOMATIS dilewati
(pakai simbol lain yang datanya sudah tersedia) -- tidak perlu keempatnya
sekaligus untuk mulai coba.

Sepenuhnya lokal, tidak butuh MT5/Telegram jalan -- cuma baca file yang
sudah diexport/dikonversi manual.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.llm_source import load_llm_cache, make_llm_classify_fn
from backtest.runner import load_signal_rows
from backtest.server_time import ServerClock
from backtest.tick_data import TickSeries
from backtest.tick_runner import BacktestConfig, build_report, run
from src.trading.symbols import SymbolResolver
from tools.run_backtest import SIGNALS_PATH, load_symbol_specs

DEFAULT_TICK_DIR = "backtest/data/ticks"
DEFAULT_LLM_CACHE = "backtest/data/llm_classify_cache.jsonl"

# canonical -> nama file default di --tick-dir (bisa dioverride per simbol
# lewat --xauusd-ticks/--nas100-ticks/--us30-ticks/--sp500-ticks)
DEFAULT_TICK_FILES = {
    "XAUUSD": "XAUUSD+_ticks.csv",
    "NAS100": "NAS100.r_ticks.csv",
    "US30": "DJ30.r_ticks.csv",
    "SP500": "SP500.r_ticks.csv",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tp-mode", choices=["p40", "channel"], default="p40")
    p.add_argument("--source", choices=["regex", "llm"], default="regex")
    p.add_argument("--llm-cache", default=DEFAULT_LLM_CACHE)
    p.add_argument("--tick-dir", default=DEFAULT_TICK_DIR, help="Folder tempat file tick disimpan.")
    p.add_argument("--xauusd-ticks", default=DEFAULT_TICK_FILES["XAUUSD"], help="Nama file tick GOLD/XAUUSD di --tick-dir.")
    p.add_argument("--nas100-ticks", default=DEFAULT_TICK_FILES["NAS100"], help="Nama file tick NAS100 di --tick-dir.")
    p.add_argument("--us30-ticks", default=DEFAULT_TICK_FILES["US30"], help="Nama file tick US30 di --tick-dir.")
    p.add_argument("--sp500-ticks", default=DEFAULT_TICK_FILES["SP500"], help="Nama file tick SP500 di --tick-dir.")
    p.add_argument(
        "--initial-deposit", type=float, default=800.0,
        help="Modal awal (USD) buat hitung max drawdown balance/equity dalam %% (default: 800).",
    )
    p.add_argument("--since", help="Batasi sinyal mulai tanggal ini (YYYY-MM-DD, inklusif), mis. 2025-03-03.")
    p.add_argument("--until", help="Batasi sinyal sebelum tanggal ini (YYYY-MM-DD, eksklusif).")
    return p.parse_args()


def main():
    args = parse_args()
    tick_files = {
        "XAUUSD": args.xauusd_ticks, "NAS100": args.nas100_ticks,
        "US30": args.us30_ticks, "SP500": args.sp500_ticks,
    }

    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)

    clock = ServerClock.from_config(settings.get("backtest"))
    print(f"Memuat data TICK... (waktu server broker = {clock.describe()}, dikoreksi ke UTC asli)")
    if not os.path.isdir(args.tick_dir):
        print(f"  [!] Folder {args.tick_dir} belum ada -- buat dulu dan taruh file tick di sana,")
        print("      atau arahkan --tick-dir ke folder yang benar. Lihat docstring modul ini.")
        return

    tick_series = {}
    for canonical, filename in tick_files.items():
        path = os.path.join(args.tick_dir, filename)
        if filename.lower().endswith(".csv"):
            if not os.path.exists(path):
                print(f"  [!] {path} tidak ada, {canonical} dilewati (butuh export manual dari MT5)")
                continue
            print(f"  Memuat {canonical} dari CSV (seluruhnya ke RAM) -- bisa berat kalau filenya besar...")
            series = TickSeries.from_csv(path, clock=clock)
        else:
            # bukan .csv -> anggap ini PREFIX hasil tools/prepare_tick_binary.py
            if not os.path.exists(path + ".times.bin"):
                print(f"  [!] {path}.times.bin tidak ada, {canonical} dilewati "
                      f"(jalankan tools/prepare_tick_binary.py dulu, atau pakai nama file .csv)")
                continue
            series = TickSeries.from_binary(path, clock=clock)
        if len(series) == 0:
            print(f"  [!] {path} kosong/tidak terbaca, {canonical} dilewati")
            continue
        tick_series[canonical] = series
        print(f"  {canonical}: {len(series):,} tick ({series.time_at(0)} - {series.time_at(len(series) - 1)})")

    if not tick_series:
        print("\nTidak ada data tick yang berhasil dimuat sama sekali -- berhenti.")
        return

    symbol_specs = load_symbol_specs()
    symbol_specs = {k: v for k, v in symbol_specs.items() if k in tick_series}

    print("\nMemuat korpus sinyal...")
    signal_rows = load_signal_rows(SIGNALS_PATH, since=args.since, until=args.until)
    range_note = f" (dibatasi {args.since or '...'} s/d {args.until or '...'})" if (args.since or args.until) else ""
    print(f"  {len(signal_rows)} pesan{range_note}")

    classify_fn = None
    if args.source == "llm":
        if not os.path.exists(args.llm_cache):
            print(f"\n[!] Cache LLM {args.llm_cache} tidak ada. Jalankan dulu:")
            print("    .venv/bin/python tools/llm_classify_corpus.py")
            return
        print(f"\nMemuat cache LLM dari {args.llm_cache}...")
        cache = load_llm_cache(args.llm_cache)
        print(f"  {len(cache)} pesan sudah terklasifikasi di cache")
        classify_fn = make_llm_classify_fn(cache)

    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )
    broker_symbols = [specs.broker_symbol for specs in symbol_specs.values()]

    tp_fixed_distance_overrides = (
        (settings.get("backtest") or {}).get("tp_fixed_distance_overrides") or {}
        if args.tp_mode == "p40" else {}
    )

    config = BacktestConfig(
        risk_usd=settings["risk"]["usd_per_trade"],
        max_lot_cap=settings["risk"]["max_lot_cap"],
        max_price_deviation_pips=settings["guards"]["max_price_deviation_pips"],
        price_deviation_overrides=settings["guards"].get("price_deviation_overrides") or {},
        min_sl_distance_overrides=settings["guards"].get("min_sl_distance_overrides") or {},
        price_offset_overrides=settings["guards"].get("broker_price_offset_overrides") or {},
        partial_close_percent=settings["followup"]["partial_close_percent"],
        move_sl_to_be_enabled=settings["followup"]["move_sl_to_be"],
        partial_close_enabled=settings["followup"]["partial_close_tp1"],
        close_all_enabled=settings["followup"]["close_all"],
        sl_plus_buffer_overrides=settings["followup"].get("sl_plus_buffer_overrides") or {},
        tp_fixed_distance_overrides=tp_fixed_distance_overrides,
    )

    print(f"\nMenjalankan simulasi presisi-tick (tp-mode={args.tp_mode}, source={args.source})...")
    trades, skipped = run(
        signal_rows=signal_rows, resolver=resolver, broker_symbols=broker_symbols,
        tick_series=tick_series, symbol_specs=symbol_specs, config=config,
        classify_fn=classify_fn,
    )

    report = build_report(trades, symbol_specs, skipped, initial_deposit=args.initial_deposit)

    print("\n" + "=" * 50)
    print("HASIL BACKTEST (PRESISI TICK)")
    print("=" * 50)
    print(f"Modal awal               : ${args.initial_deposit:,.2f}")
    if report.account_blown:
        print(
            f"🚨 AKUN HABIS (equity <= $0) pada {report.account_blown_at} -- margin call/stop-out "
            f"NYATA di broker hampir pasti kejadian LEBIH AWAL dari ini (broker mantau margin level, "
            f"bukan nunggu equity persis nol). Semua P/L SESUDAH titik ini di laporan TIDAK REALISTIS "
            f"-- modal ${args.initial_deposit:,.0f} tidak cukup buat strategi/risk ini sejauh itu."
        )
    print(f"Total trade tereksekusi : {report.total_trades}")
    print(f"  - closed              : {report.closed_trades}")
    print(f"  - masih terbuka (EOD) : {report.still_open_trades}")
    print(f"Win / Loss              : {report.wins} / {report.losses}")
    print(f"Win rate                : {report.win_rate:.1f}%")
    print(f"Total P/L               : ${report.total_pnl_usd:,.2f}")
    pf = f"{report.profit_factor:.2f}" if report.profit_factor is not None else "N/A (tidak ada loss)"
    print(f"Profit factor           : {pf}")
    print(f"Max drawdown            : ${report.max_drawdown_usd:,.2f}")
    print(f"Max DD balance          : {report.max_balance_drawdown_pct:.1f}% "
          f"(dari puncak ${report.max_balance_drawdown_peak_usd:,.2f} pada {report.max_balance_drawdown_at})")
    print(f"Max DD equity (estimasi): {report.max_equity_drawdown_pct:.1f}% "
          f"(dari puncak ${report.max_equity_drawdown_peak_usd:,.2f} pada {report.max_equity_drawdown_at})")
    print(f"Max consecutive loss    : {report.max_consecutive_losses}")
    print()
    print("Dilewati:")
    for reason, count in report.skipped.items():
        print(f"  {reason}: {count}")
    print()
    print("Per simbol:")
    for symbol, stats in sorted(report.per_symbol.items(), key=lambda kv: -kv[1]["trades"]):
        wr = (stats["wins"] / (stats["wins"] + stats["losses"]) * 100) if (stats["wins"] + stats["losses"]) else 0.0
        print(f"  {symbol:10} trades={stats['trades']:4}  win_rate={wr:5.1f}%  pnl=${stats['pnl']:,.2f}")


if __name__ == "__main__":
    main()
