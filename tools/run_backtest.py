"""Jalankan backtest sungguhan: replay tests/fixtures/signals.jsonl
terhadap data harga M5 di backtest/data/, hasilkan laporan.

    .venv/bin/python tools/run_backtest.py                       (default: TP p40)
    .venv/bin/python tools/run_backtest.py --tp-mode channel      (TP asli channel)
    .venv/bin/python tools/run_backtest.py --source llm          (pakai cache LLM,
                                                                    lihat tools/llm_classify_corpus.py)

Sepenuhnya lokal, tidak butuh MT5/Telegram — cuma baca file yang sudah ada.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.engine import SymbolSpec
from backtest.llm_source import load_llm_cache, make_llm_classify_fn
from backtest.price_data import PriceSeries
from backtest.runner import BacktestConfig, build_report, load_signal_rows, run
from src.trading.symbols import SymbolResolver

DATA_DIR = "backtest/data"
SIGNALS_PATH = "tests/fixtures/signals.jsonl"
DEFAULT_LLM_CACHE = "backtest/data/llm_classify_cache.jsonl"

# canonical -> nama file CSV di backtest/data/
PRICE_FILES = {
    "XAUUSD": "XAUUSDplus_M5.csv",
    "NAS100": "NAS100.r_M5.csv",
    "US30": "DJ30.r_M5.csv",
    "SP500": "SP500.r_M5.csv",
}


def load_symbol_specs():
    with open(os.path.join(DATA_DIR, "symbol_info.yaml")) as f:
        raw = yaml.safe_load(f)
    by_broker_symbol = {item["symbol"]: item for item in raw}

    specs = {}
    broker_symbol_map = {
        "XAUUSD": "XAUUSD+",
        "NAS100": "NAS100.r",
        "US30": "DJ30.r",
        "SP500": "SP500.r",
    }
    for canonical, broker_symbol in broker_symbol_map.items():
        item = by_broker_symbol.get(broker_symbol)
        if item is None:
            continue
        specs[canonical] = SymbolSpec(
            broker_symbol=broker_symbol,
            point=item["point"],
            digits=item["digits"],
            trade_tick_size=item["trade_tick_size"],
            trade_tick_value=item["trade_tick_value"],
            volume_step=item["volume_step"],
            volume_min=item["volume_min"],
            volume_max=item["volume_max"],
            trade_stops_level=item.get("trade_stops_level", 0.0),
        )
    return specs


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--tp-mode", choices=["p40", "channel"], default="p40",
        help="p40 = target tetap per simbol (persentil-40 gerakan historis, TERBUKTI paling profitable). "
             "channel = TP asli sesuai signal.tp channel (target terjauh, tp_index=-1) apa adanya.",
    )
    p.add_argument(
        "--source", choices=["regex", "llm"], default="regex",
        help="regex = parser regex (default, gratis & instan). "
             "llm = pakai cache hasil classify_message_with_llm (lihat tools/llm_classify_corpus.py) -- "
             "TIDAK panggil API lagi, cuma baca cache yang sudah ada.",
    )
    p.add_argument(
        "--llm-cache", default=DEFAULT_LLM_CACHE,
        help=f"Path file cache LLM (default: {DEFAULT_LLM_CACHE}), dipakai kalau --source llm.",
    )
    p.add_argument(
        "--initial-deposit", type=float, default=800.0,
        help="Modal awal (USD) buat hitung max drawdown balance/equity dalam %% (default: 800).",
    )
    return p.parse_args()


def main():
    args = parse_args()

    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)

    server_offset = (settings.get("backtest") or {}).get("server_utc_offset_hours", 0.0)
    print(f"Memuat data harga... (waktu server broker = UTC{server_offset:+g}h, dikoreksi ke UTC asli)")
    price_series = {}
    for canonical, filename in PRICE_FILES.items():
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            print(f"  [!] {path} tidak ada, {canonical} dilewati")
            continue
        series = PriceSeries.from_csv(path, server_utc_offset_hours=server_offset)
        price_series[canonical] = series
        print(f"  {canonical}: {len(series)} candle ({series.candles[0].time} - {series.candles[-1].time})")

    symbol_specs = load_symbol_specs()

    print("\nMemuat korpus sinyal...")
    signal_rows = load_signal_rows(SIGNALS_PATH)
    print(f"  {len(signal_rows)} pesan")

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

    print(f"\nMenjalankan simulasi (tp-mode={args.tp_mode}, source={args.source})...")
    trades, skipped = run(
        signal_rows=signal_rows, resolver=resolver, broker_symbols=broker_symbols,
        price_series=price_series, symbol_specs=symbol_specs, config=config,
        classify_fn=classify_fn,
    )

    report = build_report(trades, symbol_specs, skipped, initial_deposit=args.initial_deposit)

    print("\n" + "=" * 50)
    print("HASIL BACKTEST")
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
    print(f"Max DD balance          : {report.max_balance_drawdown_pct:.1f}%")
    print(f"Max DD equity (estimasi): {report.max_equity_drawdown_pct:.1f}%")
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
