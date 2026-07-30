"""Runner: replay seluruh korpus sinyal historis terhadap data harga M5,
hasilkan daftar SimulatedTrade + statistik yang dilewati (skipped) —
memakai parser, symbol resolver, dan risk sizing YANG SAMA PERSIS dengan
pipeline live (src/parser, src/trading), supaya hasilnya representatif.

Fallback LLM (MiniMax) SENGAJA TIDAK dipakai di sini — replay 6716 pesan
lewat API akan lambat & berbayar. Backtest ini hanya mencakup sinyal yang
berhasil dikenali regex (patterns.py/followup.py), bukan 100% korpus.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backtest.engine import SimulatedTrade, SymbolSpec, pnl_usd, resolve_entry_fill, resolve_trade_up_to
from backtest.price_data import PriceSeries
from src.parser.followup import parse_followup_regex
from src.parser.patterns import parse_entry_signal
from src.trading.risk import calculate_lot, calculate_partial_close_volume
from src.trading.symbols import SymbolResolver


@dataclass
class BacktestConfig:
    risk_usd: float
    max_lot_cap: float
    max_price_deviation_pips: float
    price_deviation_overrides: dict = field(default_factory=dict)
    min_sl_distance_overrides: dict = field(default_factory=dict)
    partial_close_percent: float = 50.0
    move_sl_to_be_enabled: bool = True
    partial_close_enabled: bool = True
    sl_plus_buffer_overrides: dict = field(default_factory=dict)
    tp_index: int = -1  # -1 = TP terakhir/terjauh (perilaku executor.py live saat ini).
                         # 0 = TP pertama/terdekat -- dipakai untuk bandingkan strategi.


def load_signal_rows(path: str) -> list:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["date_utc"])
    return rows


def run(
    signal_rows: list,
    resolver: SymbolResolver,
    broker_symbols: list,
    price_series: dict,
    symbol_specs: dict,
    config: BacktestConfig,
):
    trades: list = []
    open_trades_by_symbol: dict = {}
    skipped = {"symbol_not_covered": 0, "never_filled": 0, "no_sl": 0, "lot_rejected": 0}

    for row in signal_rows:
        text = row.get("text") or ""
        if not text.strip():
            continue
        date_str = row.get("date_utc")
        if not date_str:
            continue
        event_time = datetime.fromisoformat(date_str)

        signal = parse_entry_signal(text, message_id=row["message_id"])
        if signal is not None:
            canonical = resolver.canonical_of(signal.symbol)
            if canonical is None or canonical not in price_series:
                skipped["symbol_not_covered"] += 1
                continue

            series = price_series[canonical]
            spec = symbol_specs[canonical]

            if signal.sl is None:
                skipped["no_sl"] += 1
                continue

            deviation_pips = config.price_deviation_overrides.get(canonical, config.max_price_deviation_pips)
            fill = resolve_entry_fill(
                direction=signal.action, entry=signal.entry, entry_range=signal.entry_range,
                series=series, signal_time=event_time, spec=spec, max_deviation_pips=deviation_pips,
            )
            if fill is None:
                skipped["never_filled"] += 1
                continue
            fill_idx, fill_price, kind = fill

            min_sl_distance = config.min_sl_distance_overrides.get(canonical, 0.0)
            lot_result = calculate_lot(
                entry=fill_price, sl=signal.sl,
                tick_size=spec.trade_tick_size, tick_value=spec.trade_tick_value,
                volume_step=spec.volume_step, volume_min=spec.volume_min, volume_max=spec.volume_max,
                risk_usd=config.risk_usd, max_lot_cap=config.max_lot_cap,
                min_sl_distance=min_sl_distance,
            )
            if not lot_result.ok:
                skipped["lot_rejected"] += 1
                continue

            trade = SimulatedTrade(
                signal_message_id=row["message_id"], canonical_symbol=canonical,
                direction=signal.action, lot=lot_result.lot,
                entry_price=fill_price, entry_time=series.candles[fill_idx].time,
                sl=signal.sl, tp=(signal.tp[config.tp_index] if signal.tp else fill_price), kind=kind,
            )
            trade._last_resolved_index = fill_idx - 1
            trades.append(trade)
            open_trades_by_symbol.setdefault(canonical, []).append(trade)
            continue

        followup = parse_followup_regex(text, message_id=row["message_id"], reply_to_msg_id=row.get("reply_to_msg_id"))
        if followup is None or not followup.kinds or followup.symbol is None:
            continue

        canonical = resolver.canonical_of(followup.symbol)
        if canonical is None or canonical not in price_series:
            continue

        series = price_series[canonical]
        spec = symbol_specs[canonical]
        candidates = sorted(open_trades_by_symbol.get(canonical, []), key=lambda t: t.entry_time, reverse=True)

        target_trade = None
        for t in candidates:
            resolve_trade_up_to(t, series, event_time)
            if t.is_open:
                target_trade = t
                break
        if target_trade is None:
            continue

        if "move_sl_be" in followup.kinds and not target_trade.be_moved and config.move_sl_to_be_enabled:
            target_trade.sl = target_trade.entry_price
            target_trade.be_moved = True

        if "partial_close_tp1" in followup.kinds and not target_trade.tp1_hit and config.partial_close_enabled:
            pc = calculate_partial_close_volume(
                position_lot=target_trade.remaining_lot, percent=config.partial_close_percent,
                volume_step=spec.volume_step, volume_min=spec.volume_min,
            )
            if pc.ok:
                idx = series.index_at_or_after(event_time)
                approx_price = series.candles[idx].open if idx is not None else target_trade.entry_price
                if pc.action == "partial":
                    target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, pc.volume, spec)
                    target_trade.remaining_lot -= pc.volume

                    # SL+ otomatis: begitu TP1/partial-close berhasil, SL dipindah ke
                    # breakeven+buffer -- SAMA PERSIS dgn logika main.py live, tidak
                    # bergantung apakah followup.kinds juga mengandung move_sl_be.
                    buffer = config.sl_plus_buffer_overrides.get(canonical, 0.0)
                    if buffer > 0:
                        target_trade.sl = (
                            target_trade.entry_price + buffer
                            if target_trade.direction == "BUY"
                            else target_trade.entry_price - buffer
                        )
                        target_trade.be_moved = True
                else:
                    target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, target_trade.remaining_lot, spec)
                    target_trade.remaining_lot = 0.0
                    target_trade.exit_price = approx_price
                    target_trade.exit_time = event_time
                    target_trade.exit_reason = "partial_close_full"
                target_trade.tp1_hit = True

    for canonical, series in price_series.items():
        if not series.candles:
            continue
        last_time = series.candles[-1].time
        for t in open_trades_by_symbol.get(canonical, []):
            resolve_trade_up_to(t, series, last_time)

    return trades, skipped


@dataclass
class BacktestReport:
    total_trades: int
    closed_trades: int
    still_open_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    per_symbol: dict
    skipped: dict


def build_report(trades: list, symbol_specs: dict, skipped: dict) -> BacktestReport:
    wins = 0
    losses = 0
    total_pnl = 0.0
    closed_count = 0
    open_count = 0
    per_symbol: dict = {}

    for t in trades:
        spec = symbol_specs[t.canonical_symbol]
        stats = per_symbol.setdefault(t.canonical_symbol, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        stats["trades"] += 1

        pnl = t.realized_pnl_usd
        if t.exit_reason in ("tp", "sl", "partial_close_full"):
            if t.remaining_lot > 0:
                pnl += pnl_usd(t, t.exit_price, t.remaining_lot, spec)
            closed_count += 1
            if pnl > 0:
                wins += 1
                stats["wins"] += 1
            else:
                losses += 1
                stats["losses"] += 1
        else:
            open_count += 1

        total_pnl += pnl
        stats["pnl"] += pnl

    win_rate = (wins / closed_count * 100) if closed_count else 0.0

    return BacktestReport(
        total_trades=len(trades),
        closed_trades=closed_count,
        still_open_trades=open_count,
        wins=wins, losses=losses, win_rate=win_rate,
        total_pnl_usd=total_pnl,
        per_symbol=per_symbol,
        skipped=skipped,
    )
