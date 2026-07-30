"""Versi TICK-PRESISI dari backtest/runner.py -- replay korpus sinyal
terhadap data TICK asli (bukan candle M5). Sama persis strukturnya dengan
runner.py (termasuk pencocokan follow-up lewat reply_to_msg_id yang sudah
terbukti menaikkan akurasi signifikan), cuma sumber harganya beda -- lihat
tick_engine.py utk kenapa ini menghilangkan ambiguitas "SL/TP kena di
candle yang sama".
"""

from datetime import datetime

from backtest.engine import pnl_usd
from backtest.runner import BacktestConfig, BacktestReport, _resolve_trade_via_reply_chain, build_report
from backtest.tick_data import TickSeries
from backtest.tick_engine import SimulatedTrade, resolve_entry_fill_tick, resolve_trade_up_to_tick
from src.parser.followup import classify_followup_kinds, parse_followup_regex
from src.parser.patterns import parse_entry_signal
from src.parser.schema import FollowUp, Signal, apply_price_offset
from src.trading.risk import calculate_lot, calculate_partial_close_volume
from src.trading.symbols import SymbolResolver

__all__ = ["BacktestConfig", "BacktestReport", "build_report", "run"]


def _close_price(series: TickSeries, idx: int, direction: str) -> float:
    """Harga yang dipakai utk MENUTUP posisi pada index tick tsb -- BUY
    ditutup dgn SELL (dapat BID), SELL ditutup dgn BUY (bayar ASK)."""
    return series.bids[idx] if direction == "BUY" else series.asks[idx]


def run(
    signal_rows: list,
    resolver: SymbolResolver,
    broker_symbols: list,
    tick_series: dict,
    symbol_specs: dict,
    config: BacktestConfig,
    classify_fn=None,
):
    """classify_fn: lihat docstring backtest/runner.py:run -- sama persis
    perilakunya di sini, cuma harganya presisi tick."""
    trades: list = []
    open_trades_by_symbol: dict = {}
    trade_by_message_id: dict = {}
    rows_by_id = {row["message_id"]: row for row in signal_rows}
    skipped = {"symbol_not_covered": 0, "never_filled": 0, "no_sl": 0, "lot_rejected": 0}

    for row in signal_rows:
        text = row.get("text") or ""
        if not text.strip():
            continue
        date_str = row.get("date_utc")
        if not date_str:
            continue
        event_time = datetime.fromisoformat(date_str)

        if classify_fn is not None:
            classified = classify_fn(text, row["message_id"], row.get("reply_to_msg_id"))
            signal = classified if isinstance(classified, Signal) else None
            preclassified_followup = classified if isinstance(classified, FollowUp) else None
            if signal is None and preclassified_followup is None:
                continue
        else:
            signal = parse_entry_signal(text, message_id=row["message_id"])
            preclassified_followup = None

        if signal is not None:
            canonical = resolver.canonical_of(signal.symbol)
            if canonical is None or canonical not in tick_series:
                skipped["symbol_not_covered"] += 1
                continue

            series = tick_series[canonical]
            spec = symbol_specs[canonical]

            offset = config.price_offset_overrides.get(canonical, 0.0)
            signal = apply_price_offset(signal, offset)

            if signal.sl is None:
                skipped["no_sl"] += 1
                continue

            deviation_pips = config.price_deviation_overrides.get(canonical, config.max_price_deviation_pips)
            fill = resolve_entry_fill_tick(
                direction=signal.action, entry=signal.entry, entry_range=signal.entry_range,
                tick_series=series, signal_time=event_time, spec=spec, max_deviation_pips=deviation_pips,
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

            r_value = abs(fill_price - signal.sl)
            fixed_distance = config.tp_fixed_distance_overrides.get(canonical)
            if config.tp_r_multiple is not None:
                tp_price = (
                    fill_price + config.tp_r_multiple * r_value
                    if signal.action == "BUY"
                    else fill_price - config.tp_r_multiple * r_value
                )
            elif fixed_distance is not None:
                tp_price = fill_price + fixed_distance if signal.action == "BUY" else fill_price - fixed_distance
            else:
                tp_price = signal.tp[config.tp_index] if signal.tp else fill_price

            trade = SimulatedTrade(
                signal_message_id=row["message_id"], canonical_symbol=canonical,
                direction=signal.action, lot=lot_result.lot,
                entry_price=fill_price, entry_time=series.time_at(fill_idx),
                sl=signal.sl, tp=tp_price, kind=kind, r_value=r_value,
            )
            trade._last_resolved_index = fill_idx - 1
            trades.append(trade)
            open_trades_by_symbol.setdefault(canonical, []).append(trade)
            trade_by_message_id[row["message_id"]] = trade
            continue

        if classify_fn is not None:
            followup = preclassified_followup  # dijamin bukan None (lihat guard di atas)
        else:
            followup = parse_followup_regex(text, message_id=row["message_id"], reply_to_msg_id=row.get("reply_to_msg_id"))
        target_trade = None
        kinds: list = []

        if followup is not None:
            if not followup.kinds:
                continue
            kinds = followup.kinds
            target_trade = _resolve_trade_via_reply_chain(row.get("reply_to_msg_id"), rows_by_id, trade_by_message_id)
            if target_trade is not None:
                canonical = target_trade.canonical_symbol
                series = tick_series[canonical]
                spec = symbol_specs[canonical]
                resolve_trade_up_to_tick(target_trade, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
                if not target_trade.is_open:
                    target_trade = None
            if target_trade is None:
                if followup.symbol is None:
                    continue
                canonical = resolver.canonical_of(followup.symbol)
                if canonical is None or canonical not in tick_series:
                    continue
                series = tick_series[canonical]
                spec = symbol_specs[canonical]
                candidates = sorted(open_trades_by_symbol.get(canonical, []), key=lambda t: t.entry_time, reverse=True)
                for t in candidates:
                    resolve_trade_up_to_tick(t, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
                    if t.is_open:
                        target_trade = t
                        break
                if target_trade is None:
                    continue
        else:
            target_trade = _resolve_trade_via_reply_chain(row.get("reply_to_msg_id"), rows_by_id, trade_by_message_id)
            if target_trade is None:
                continue
            canonical = target_trade.canonical_symbol
            series = tick_series[canonical]
            spec = symbol_specs[canonical]
            resolve_trade_up_to_tick(target_trade, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
            if not target_trade.is_open:
                continue
            kinds = classify_followup_kinds(text)
            if not kinds:
                continue

        trade_by_message_id[row["message_id"]] = target_trade

        if "move_sl_be" in kinds and not target_trade.be_moved and config.move_sl_to_be_enabled:
            target_trade.sl = target_trade.entry_price
            target_trade.be_moved = True

        if "partial_close_tp1" in kinds and not target_trade.tp1_hit and config.partial_close_enabled:
            pc = calculate_partial_close_volume(
                position_lot=target_trade.remaining_lot, percent=config.partial_close_percent,
                volume_step=spec.volume_step, volume_min=spec.volume_min,
            )
            if pc.ok:
                idx = series.index_at_or_after(event_time)
                approx_price = _close_price(series, idx, target_trade.direction) if idx is not None else target_trade.entry_price
                if pc.action == "partial":
                    target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, pc.volume, spec)
                    target_trade.remaining_lot -= pc.volume

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

        if "close_all" in kinds and target_trade.is_open and config.close_all_enabled:
            idx = series.index_at_or_after(event_time)
            approx_price = _close_price(series, idx, target_trade.direction) if idx is not None else target_trade.entry_price
            target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, target_trade.remaining_lot, spec)
            target_trade.remaining_lot = 0.0
            target_trade.exit_price = approx_price
            target_trade.exit_time = event_time
            target_trade.exit_reason = "close_all"

    for canonical, series in tick_series.items():
        if len(series) == 0:
            continue
        last_time = series.time_at(len(series) - 1)
        for t in open_trades_by_symbol.get(canonical, []):
            resolve_trade_up_to_tick(t, series, last_time, auto_be_r_multiple=config.auto_be_r_multiple)

    return trades, skipped
