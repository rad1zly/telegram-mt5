"""Cari strategi TP paling menguntungkan dengan menguji BANYAK kandidat
pada data yang sama, lalu bandingkan berdampingan.

    .venv/bin/python tools/sweep_tp.py
    .venv/bin/python tools/sweep_tp.py --since 2025-03-03
    .venv/bin/python tools/sweep_tp.py --sort pf          (urutkan by profit factor)
    .venv/bin/python tools/sweep_tp.py --ticks            (pakai data TICK, bukan M5)

Kandidat yang diuji:
  - TP channel apa adanya (target terjauh & target terdekat)
  - TP kelipatan R (entry +/- N x jarak-ke-SL), self-scaling per trade
  - TP jarak TETAP per simbol, diturunkan dari PERSENTIL distribusi MFE
    ("gerakan terbaik sebelum SL kena") yang DIHITUNG ULANG dari data ini
    -- bukan angka hardcode, jadi ikut benar kalau datanya berubah.

Kenapa tidak cuma lihat total P/L: strategi dengan P/L tertinggi bisa
punya drawdown 2x lipat lebih dalam. Tabel hasil menampilkan P/L, win
rate, profit factor, DAN max drawdown sekaligus supaya trade-off-nya
kelihatan. Kolom terakhir (P/L per %DD) adalah ukuran risk-adjusted
sederhana: berapa dolar profit per 1% drawdown yang harus ditanggung.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.price_data import PriceSeries
from backtest.runner import BacktestConfig, build_report, load_signal_rows
from backtest.runner import run as run_m5
from backtest.server_time import ServerClock
from src.parser.patterns import parse_entry_signal
from src.parser.schema import apply_price_offset
from src.trading.symbols import SymbolResolver
from tools.run_backtest import DATA_DIR, PRICE_FILES, SIGNALS_PATH, load_symbol_specs

MFE_PERCENTILES = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70)
R_MULTIPLES = (1.0, 1.5, 2.0, 3.0, 4.0)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", help="Batasi sinyal mulai tanggal ini (YYYY-MM-DD, inklusif).")
    p.add_argument("--until", help="Batasi sinyal sebelum tanggal ini (YYYY-MM-DD, eksklusif).")
    p.add_argument("--initial-deposit", type=float, default=800.0)
    p.add_argument("--sort", choices=["pnl", "pf", "dd", "adj"], default="adj",
                   help="Urutkan hasil by: pnl=total profit, pf=profit factor, "
                        "dd=drawdown terkecil, adj=P/L per %%DD (default, risk-adjusted).")
    p.add_argument("--ticks", action="store_true",
                   help="Pakai data TICK (backtest/data/ticks/ atau --tick-dir) alih-alih candle M5.")
    p.add_argument("--tick-dir", default="backtest/data/ticks")
    return p.parse_args()


def compute_mfe_percentiles(price_series, symbol_specs, resolver, signal_rows, config, percentiles):
    """Distribusi MFE = untuk tiap sinyal historis, seberapa jauh harga
    sempat bergerak MENGUNTUNGKAN sebelum SL kena. Persentil-nya jadi
    kandidat jarak TP yang 'realistis dicapai' -- diturunkan dari data
    yang sedang diuji, bukan angka warisan yang mungkin sudah basi."""
    broker_symbols = [spec.broker_symbol for spec in symbol_specs.values()]
    mfe_by_symbol = {}

    for row in signal_rows:
        signal = parse_entry_signal(row["text"] or "", message_id=row["message_id"])
        if signal is None or signal.sl is None:
            continue
        resolved = resolver.resolve(signal.symbol, broker_symbols)
        if not resolved.ok or resolved.canonical not in price_series:
            continue

        canonical = resolved.canonical
        signal = apply_price_offset(signal, config.price_offset_overrides.get(canonical, 0.0))
        series = price_series[canonical]
        from datetime import datetime

        idx = series.index_at_or_after(datetime.fromisoformat(row["date_utc"]))
        if idx is None:
            continue

        entry = series.candles[idx].open
        is_buy = signal.action == "BUY"
        best = 0.0
        # 2000 candle M5 ~= 1 minggu perdagangan; cukup panjang utk menangkap
        # gerakan penuh tanpa menyeret trade lama ke rezim pasar lain.
        for candle in series.candles[idx:idx + 2000]:
            if is_buy and candle.low <= signal.sl:
                break
            if not is_buy and candle.high >= signal.sl:
                break
            favorable = (candle.high - entry) if is_buy else (entry - candle.low)
            best = max(best, favorable)
        if best > 0:
            mfe_by_symbol.setdefault(canonical, []).append(best)

    result = {}
    for pct in percentiles:
        distances = {}
        for canonical, values in mfe_by_symbol.items():
            values = sorted(values)
            if values:
                distances[canonical] = round(values[int(len(values) * pct)], 2)
        if distances:
            result[pct] = distances
    return result


def build_candidates(base_settings, mfe_levels, existing_overrides):
    """[(label, dict-kwargs-utk-BacktestConfig)]"""
    candidates = [
        ("TP channel (terjauh)", {"tp_index": -1}),
        ("TP channel (terdekat)", {"tp_index": 0}),
    ]
    for r in R_MULTIPLES:
        candidates.append((f"R-multiple {r}R", {"tp_r_multiple": r}))
    for pct, distances in sorted(mfe_levels.items()):
        label = f"MFE p{int(pct * 100)} {distances}"
        candidates.append((f"MFE p{int(pct * 100)}", {"tp_fixed_distance_overrides": distances}))
    if existing_overrides:
        candidates.append(("config saat ini", {"tp_fixed_distance_overrides": dict(existing_overrides)}))
    return candidates


def make_config(settings, **overrides):
    kwargs = dict(
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
    )
    kwargs.update(overrides)
    return BacktestConfig(**kwargs)


def main():
    args = parse_args()
    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)

    clock = ServerClock.from_config(settings.get("backtest"))
    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )

    print(f"Memuat data... (server broker {clock.describe()})")
    symbol_specs = load_symbol_specs()

    if args.ticks:
        from backtest.tick_data import TickSeries
        from backtest.tick_runner import run as run_tick

        series_map = {}
        for canonical in list(symbol_specs):
            base = os.path.join(args.tick_dir, canonical)
            if os.path.exists(base + ".times.bin"):
                series_map[canonical] = TickSeries.from_binary(base, clock=clock)
            elif os.path.exists(base + ".csv"):
                series_map[canonical] = TickSeries.from_csv(base + ".csv", clock=clock)
        if not series_map:
            print(f"[!] Tidak ada data tick di {args.tick_dir} (cari <SIMBOL>.times.bin atau <SIMBOL>.csv).")
            return
        runner, series_kwarg = run_tick, "tick_series"
    else:
        series_map = {}
        for canonical, filename in PRICE_FILES.items():
            path = os.path.join(DATA_DIR, filename)
            if os.path.exists(path):
                series_map[canonical] = PriceSeries.from_csv(path, clock=clock)
        runner, series_kwarg = run_m5, "price_series"

    symbol_specs = {k: v for k, v in symbol_specs.items() if k in series_map}
    broker_symbols = [spec.broker_symbol for spec in symbol_specs.values()]
    for canonical in sorted(series_map):
        print(f"  {canonical}: {len(series_map[canonical]):,} titik data")

    signal_rows = load_signal_rows(SIGNALS_PATH, since=args.since, until=args.until)
    range_note = f" (dibatasi {args.since or '...'} s/d {args.until or '...'})" if (args.since or args.until) else ""
    print(f"\n{len(signal_rows)} pesan{range_note}")

    base_config = make_config(settings)
    if args.ticks:
        # MFE dihitung dari candle M5 (butuh .candles); utk mode tick,
        # pakai persentil dari config saja supaya tidak menebak-nebak.
        print("\nMode tick: persentil MFE tidak dihitung ulang (butuh candle M5).")
        mfe_levels = {}
    else:
        print("\nMenghitung distribusi MFE dari data ini...")
        mfe_levels = compute_mfe_percentiles(
            series_map, symbol_specs, resolver, signal_rows, base_config, MFE_PERCENTILES
        )
        for pct, distances in sorted(mfe_levels.items()):
            print(f"  p{int(pct * 100)}: {distances}")

    existing = (settings.get("backtest") or {}).get("tp_fixed_distance_overrides") or {}
    candidates = build_candidates(settings, mfe_levels, existing)

    print(f"\nMenguji {len(candidates)} kandidat TP...\n")
    results = []
    for label, overrides in candidates:
        config = make_config(settings, **overrides)
        trades, skipped = runner(
            signal_rows=signal_rows, resolver=resolver, broker_symbols=broker_symbols,
            symbol_specs=symbol_specs, config=config, **{series_kwarg: series_map},
        )
        report = build_report(trades, symbol_specs, skipped, initial_deposit=args.initial_deposit)
        # P/L per 1% drawdown -- ukuran risk-adjusted sederhana. DD 0 berarti
        # tidak pernah turun sama sekali; pakai None supaya tidak dibagi nol.
        adjusted = (report.total_pnl_usd / report.max_balance_drawdown_pct
                    if report.max_balance_drawdown_pct > 0 else None)
        results.append({
            "label": label, "report": report, "adjusted": adjusted,
            "blown": report.account_blown,
        })

    sort_key = {
        "pnl": lambda r: -r["report"].total_pnl_usd,
        "pf": lambda r: -(r["report"].profit_factor or 0),
        "dd": lambda r: r["report"].max_balance_drawdown_pct,
        "adj": lambda r: -(r["adjusted"] if r["adjusted"] is not None else -1e9),
    }[args.sort]
    results.sort(key=sort_key)

    print("=" * 96)
    print(f"HASIL SWEEP TP (modal ${args.initial_deposit:,.0f}, diurutkan by {args.sort})")
    print("=" * 96)
    print(f"{'strategi':24} {'P/L':>11} {'WR':>7} {'PF':>6} {'maxDD':>7} {'beruntun':>9} {'$/1%DD':>9}")
    print("-" * 96)
    for r in results:
        rep = r["report"]
        pf = f"{rep.profit_factor:.2f}" if rep.profit_factor else "n/a"
        adj = f"{r['adjusted']:,.0f}" if r["adjusted"] is not None else "-"
        warn = "  [AKUN HABIS]" if r["blown"] else ""
        print(f"{r['label']:24} {rep.total_pnl_usd:>11,.2f} {rep.win_rate:>6.1f}% {pf:>6} "
              f"{rep.max_balance_drawdown_pct:>6.1f}% {rep.max_consecutive_losses:>9} {adj:>9}{warn}")

    best = results[0]
    print("\n" + "=" * 96)
    print(f"TERBAIK menurut '{args.sort}': {best['label']}")
    rep = best["report"]
    print(f"  P/L ${rep.total_pnl_usd:,.2f} | WR {rep.win_rate:.1f}% | "
          f"PF {rep.profit_factor:.2f} | maxDD {rep.max_balance_drawdown_pct:.1f}%")
    print("\nCATATAN: ini hasil pada SATU periode historis. Strategi yang menang tipis")
    print("belum tentu benar-benar lebih baik -- perhatikan juga apakah unggulnya")
    print("konsisten saat --since/--until digeser ke sub-periode lain.")


if __name__ == "__main__":
    main()
