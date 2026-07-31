"""Versi TICK-PRESISI dari backtest/engine.py -- dipakai kalau ada data
tick asli (bukan cuma candle M5). Logika bisnis (SymbolSpec, SimulatedTrade,
pnl_usd, compute_market_tolerance, decide_order_kind) dipakai ULANG PERSIS
dari engine.py -- yang beda:

- Deteksi fill pending order & SL/TP hit pakai OPERASI NUMPY VEKTOR (bukan
  loop Python per tick) -- WAJIB, karena file tick riil bisa berisi ratusan
  juta baris; loop Python murni per tick akan terlalu lambat pada skala itu.
- TIDAK ADA lagi asumsi konservatif "kalau SL dan TP dua-duanya kena di
  candle yang sama, anggap SL duluan" -- tiap tick presisi waktu asli, jadi
  urutan kejadian selalu tepat (tie-break SL-duluan cuma relevan kalau
  SL==TP persis, kasus yg nyaris mustahil).
- Sisi harga yang dipakai mengikuti konvensi broker sungguhan: BUY dicek
  terhadap ASK (harga beli), SELL dicek terhadap BID (harga jual) --
  sama seperti mt5_client.get_current_price().
"""

from datetime import datetime
from typing import Optional

import numpy as np

from backtest.engine import SimulatedTrade, SymbolSpec, pnl_usd  # noqa: F401 (re-export)
from backtest.tick_data import TickSeries
from src.trading.mt5_client import compute_market_tolerance, decide_order_kind


def resolve_entry_fill_tick(
    direction: str,
    entry: Optional[float],
    entry_range: Optional[tuple],
    tick_series: TickSeries,
    signal_time: datetime,
    spec: SymbolSpec,
    max_deviation_pips: float,
):
    """Sama seperti engine.py:resolve_entry_fill, tapi cari tick pertama yg
    menyentuh level target lewat vectorized numpy (bukan scan Python).
    Return (tick_index, fill_price, kind) atau None."""
    start_idx = tick_series.index_at_or_after(signal_time)
    if start_idx is None:
        return None

    is_buy = direction == "BUY"
    current_price = tick_series.asks[start_idx] if is_buy else tick_series.bids[start_idx]

    if entry is not None:
        target = entry
    elif entry_range is not None:
        low, high = entry_range
        if low <= current_price <= high:
            target = current_price
        elif current_price < low:
            target = low
        else:
            target = high
    else:
        target = current_price  # "Buy/Sell Now" polos, tidak ada level spesifik

    tolerance = compute_market_tolerance(spec, max_deviation_pips)
    kind = decide_order_kind(direction, target, current_price, tolerance)

    if kind == "MARKET":
        return start_idx, current_price, kind

    # Pending order (STOP) -- BUY dicek dgn ASK, SELL dgn BID. Hanya berlaku
    # sampai akhir hari sinyalnya, alasan sama persis dgn engine.py candle:
    # channel kirim sinyal baru tiap hari, pending yang belum tersentuh
    # sampai pergantian hari sudah tidak relevan.
    deadline = signal_time.replace(hour=23, minute=59, second=59, microsecond=999999)
    end_idx = tick_series.index_at_or_after(deadline)
    if end_idx is None:
        end_idx = len(tick_series)

    prices = tick_series.asks if is_buy else tick_series.bids
    subset = prices[start_idx:end_idx]
    if kind == "BUY_STOP":
        mask = subset >= target  # nunggu harga NAIK menembus target
    else:  # SELL_STOP -- nunggu harga TURUN menembus target
        mask = subset <= target
    hits = np.nonzero(mask)[0]
    if len(hits) == 0:
        return None  # kedaluwarsa/tidak pernah tersentuh
    return start_idx + int(hits[0]), target, kind


def _first_sl_tp_hit(close_prices: np.ndarray, lo: int, hi: int, is_buy: bool, sl: float, tp: float):
    """Cari hit PERTAMA (index) dalam rentang [lo, hi) -- SL dicek duluan
    sbg tie-break kalau SL==TP persis (kasus yg nyaris mustahil, tapi
    konsisten dgn engine.py candle). Return (index, 'sl'|'tp') atau None."""
    if lo >= hi:
        return None
    subset = close_prices[lo:hi]
    sl_mask = (subset <= sl) if is_buy else (subset >= sl)
    tp_mask = (subset >= tp) if is_buy else (subset <= tp)
    sl_hits = np.nonzero(sl_mask)[0]
    tp_hits = np.nonzero(tp_mask)[0]
    sl_first = lo + int(sl_hits[0]) if len(sl_hits) else None
    tp_first = lo + int(tp_hits[0]) if len(tp_hits) else None
    if sl_first is None and tp_first is None:
        return None
    if sl_first is None:
        return tp_first, "tp"
    if tp_first is None:
        return sl_first, "sl"
    return (sl_first, "sl") if sl_first <= tp_first else (tp_first, "tp")


def resolve_trade_up_to_tick(
    trade: SimulatedTrade,
    tick_series: TickSeries,
    up_to_time: datetime,
    auto_be_r_multiple: Optional[float] = None,
) -> None:
    """Sama seperti engine.py:resolve_trade_up_to, tapi vectorized numpy --
    lihat docstring modul. Kalau auto_be_r_multiple diisi dan belum
    be_moved, dicek dalam 2 fase (sebelum & sesudah SL pindah ke entry)
    karena level SL berubah di tengah jalan."""
    if trade.is_closed:
        return

    if trade._last_resolved_index == -1:
        start_idx = tick_series.index_at_or_after(trade.entry_time)
        if start_idx is None:
            trade.exit_reason = "still_open"
            return
    else:
        start_idx = trade._last_resolved_index + 1

    end_idx = tick_series.index_at_or_after(up_to_time)
    if end_idx is None:
        end_idx = len(tick_series)

    if start_idx >= end_idx:
        trade._last_resolved_index = start_idx - 1
        return

    is_buy = trade.direction == "BUY"
    # Harga relevan utk menutup posisi: BUY ditutup dgn SELL (dapat BID),
    # SELL ditutup dgn BUY (bayar ASK) -- kebalikan dari sisi saat membuka.
    close_prices = tick_series.bids if is_buy else tick_series.asks

    be_idx = None
    if auto_be_r_multiple is not None and trade.r_value and not trade.be_moved:
        favorable = (close_prices - trade.entry_price) if is_buy else (trade.entry_price - close_prices)
        threshold = auto_be_r_multiple * trade.r_value
        be_hits = np.nonzero(favorable[start_idx:end_idx] >= threshold)[0]
        if len(be_hits):
            be_idx = start_idx + int(be_hits[0])

    if be_idx is not None:
        # Fase 1: cek SL/TP SEBELUM auto-BE trigger, pakai SL LAMA.
        result = _first_sl_tp_hit(close_prices, start_idx, be_idx, is_buy, trade.sl, trade.tp)
        if result is not None:
            idx, reason = result
            trade.exit_price = trade.sl if reason == "sl" else trade.tp
            trade.exit_time = tick_series.time_at(idx)
            trade.exit_reason = reason
            trade._last_resolved_index = idx
            return
        # Auto-BE trigger tepat di be_idx.
        trade.sl = trade.entry_price
        trade.be_moved = True
        # Fase 2: lanjut cek dari be_idx pakai SL BARU (breakeven).
        result = _first_sl_tp_hit(close_prices, be_idx, end_idx, is_buy, trade.sl, trade.tp)
        if result is not None:
            idx, reason = result
            trade.exit_price = trade.sl if reason == "sl" else trade.tp
            trade.exit_time = tick_series.time_at(idx)
            trade.exit_reason = reason
            trade._last_resolved_index = idx
            return
        trade._last_resolved_index = end_idx - 1
        return

    result = _first_sl_tp_hit(close_prices, start_idx, end_idx, is_buy, trade.sl, trade.tp)
    if result is not None:
        idx, reason = result
        trade.exit_price = trade.sl if reason == "sl" else trade.tp
        trade.exit_time = tick_series.time_at(idx)
        trade.exit_reason = reason
        trade._last_resolved_index = idx
        return
    trade._last_resolved_index = end_idx - 1
