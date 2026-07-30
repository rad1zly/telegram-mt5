"""Jalankan backtest sungguhan: replay tests/fixtures/signals.jsonl
terhadap data harga M5 di backtest/data/, hasilkan laporan.

    .venv/bin/python tools/run_backtest.py     (atau python biasa di Windows)

Sepenuhnya lokal, tidak butuh MT5/Telegram — cuma baca file yang sudah ada.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from backtest.engine import SymbolSpec
from backtest.price_data import PriceSeries
from backtest.runner import BacktestConfig, build_report, load_signal_rows, run
from src.trading.symbols import SymbolResolver

DATA_DIR = "backtest/data"
SIGNALS_PATH = "tests/fixtures/signals.jsonl"

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


def main():
    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)

    print("Memuat data harga...")
    price_series = {}
    for canonical, filename in PRICE_FILES.items():
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            print(f"  [!] {path} tidak ada, {canonical} dilewati")
            continue
        series = PriceSeries.from_csv(path)
        price_series[canonical] = series
        print(f"  {canonical}: {len(series)} candle ({series.candles[0].time} - {series.candles[-1].time})")

    symbol_specs = load_symbol_specs()

    print("\nMemuat korpus sinyal...")
    signal_rows = load_signal_rows(SIGNALS_PATH)
    print(f"  {len(signal_rows)} pesan")

    resolver = SymbolResolver(
        settings["symbols"]["aliases"],
        settings["symbols"].get("broker_overrides") or {},
    )
    broker_symbols = [specs.broker_symbol for specs in symbol_specs.values()]

    config = BacktestConfig(
        risk_usd=settings["risk"]["usd_per_trade"],
        max_lot_cap=settings["risk"]["max_lot_cap"],
        max_price_deviation_pips=settings["guards"]["max_price_deviation_pips"],
        price_deviation_overrides=settings["guards"].get("price_deviation_overrides") or {},
        min_sl_distance_overrides=settings["guards"].get("min_sl_distance_overrides") or {},
        partial_close_percent=settings["followup"]["partial_close_percent"],
        move_sl_to_be_enabled=settings["followup"]["move_sl_to_be"],
        partial_close_enabled=settings["followup"]["partial_close_tp1"],
        sl_plus_buffer_overrides=settings["followup"].get("sl_plus_buffer_overrides") or {},
    )

    print("\nMenjalankan simulasi...")
    trades, skipped = run(
        signal_rows=signal_rows, resolver=resolver, broker_symbols=broker_symbols,
        price_series=price_series, symbol_specs=symbol_specs, config=config,
    )

    report = build_report(trades, symbol_specs, skipped)

    print("\n" + "=" * 50)
    print("HASIL BACKTEST")
    print("=" * 50)
    print(f"Total trade tereksekusi : {report.total_trades}")
    print(f"  - closed              : {report.closed_trades}")
    print(f"  - masih terbuka (EOD) : {report.still_open_trades}")
    print(f"Win / Loss              : {report.wins} / {report.losses}")
    print(f"Win rate                : {report.win_rate:.1f}%")
    print(f"Total P/L               : ${report.total_pnl_usd:,.2f}")
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
