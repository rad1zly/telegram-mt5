"""Versi TICK-PRESISI dari backtest/engine.py -- dipakai kalau ada data
tick asli (bukan cuma candle M5). Logika bisnis (SymbolSpec, SimulatedTrade,
pnl_usd, compute_market_tolerance, decide_order_kind) dipakai ULANG PERSIS
dari engine.py -- yang beda cuma cara membaca harga: tiap TICK adalah satu
titik harga presisi (bid/ask, bukan OHLC per interval), jadi:

- Deteksi fill pending order & SL/TP HIT tidak butuh scan "high/low per
  candle" lagi -- cukup bandingkan harga tick langsung ke level target.
- TIDAK ADA lagi asumsi konservatif "kalau SL dan TP dua-duanya kena di
  candle yang sama, anggap SL duluan" -- tiap tick diproses satu-satu
  sesuai urutan waktu ASLI, jadi urutan kejadian selalu presisi.
- Sisi harga yang dipakai mengikuti konvensi broker sungguhan: BUY dicek
  terhadap ASK (harga beli), SELL dicek terhadap BID (harga jual) --
  sama seperti mt5_client.get_current_price().
"""

from datetime import datetime
from typing import Optional

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
    """Sama seperti engine.py:resolve_entry_fill, tapi scan tick demi tick.
    Return (tick_index, fill_price, kind) atau None (tidak ada data / order
    pending tidak pernah tersentuh sampai akhir data)."""
    start_idx = tick_series.index_at_or_after(signal_time)
    if start_idx is None:
        return None

    is_buy = direction == "BUY"
    current_tick = tick_series.ticks[start_idx]
    current_price = current_tick.ask if is_buy else current_tick.bid

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

    # Pending order -- scan maju tick demi tick sampai level target tersentuh.
    # BUY (STOP maupun LIMIT) dicek dgn ASK (harga beli), SELL dgn BID.
    for i in range(start_idx, len(tick_series.ticks)):
        tick = tick_series.ticks[i]
        relevant_price = tick.ask if is_buy else tick.bid
        if kind in ("BUY_STOP", "SELL_LIMIT"):
            if relevant_price >= target:
                return i, target, kind
        else:  # BUY_LIMIT, SELL_STOP
            if relevant_price <= target:
                return i, target, kind

    return None  # tidak pernah tersentuh sampai akhir data


def resolve_trade_up_to_tick(
    trade: SimulatedTrade,
    tick_series: TickSeries,
    up_to_time: datetime,
    auto_be_r_multiple: Optional[float] = None,
) -> None:
    """Sama seperti engine.py:resolve_trade_up_to, tapi tick demi tick --
    lihat docstring modul soal kenapa ini menghilangkan ambiguitas
    'SL/TP kena di candle yang sama'."""
    if trade.is_closed:
        return

    if trade._last_resolved_index == -1:
        start_idx = tick_series.index_at_or_after(trade.entry_time)
        if start_idx is None:
            trade.exit_reason = "still_open"
            return
    else:
        start_idx = trade._last_resolved_index + 1

    is_buy = trade.direction == "BUY"

    for i in range(start_idx, len(tick_series.ticks)):
        tick = tick_series.ticks[i]
        if tick.time > up_to_time:
            trade._last_resolved_index = i - 1
            return

        # Harga relevan utk menutup posisi: BUY ditutup dgn SELL (dapat
        # BID), SELL ditutup dgn BUY (bayar ASK) -- kebalikan dari sisi
        # yang dipakai saat MEMBUKA posisi.
        relevant_price = tick.bid if is_buy else tick.ask

        if auto_be_r_multiple is not None and trade.r_value and not trade.be_moved:
            favorable = (relevant_price - trade.entry_price) if is_buy else (trade.entry_price - relevant_price)
            if favorable >= auto_be_r_multiple * trade.r_value:
                trade.sl = trade.entry_price
                trade.be_moved = True

        sl_hit = (relevant_price <= trade.sl) if is_buy else (relevant_price >= trade.sl)
        tp_hit = (relevant_price >= trade.tp) if is_buy else (relevant_price <= trade.tp)

        if sl_hit:
            trade.exit_price = trade.sl
            trade.exit_time = tick.time
            trade.exit_reason = "sl"
            trade._last_resolved_index = i
            return
        if tp_hit:
            trade.exit_price = trade.tp
            trade.exit_time = tick.time
            trade.exit_reason = "tp"
            trade._last_resolved_index = i
            return

    trade._last_resolved_index = len(tick_series.ticks) - 1
