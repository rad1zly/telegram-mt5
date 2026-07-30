"""Engine simulasi backtest: replay sinyal historis terhadap data harga
M5 asli. Logika entry-fill, market-vs-pending, dan lot sizing SENGAJA
memakai fungsi yang SAMA PERSIS dengan pipeline live (src/trading/*),
supaya hasil backtest merepresentasikan bot yang sungguhan, bukan model
simulasi terpisah yang bisa diam-diam berbeda perilakunya.

Penyederhanaan yang disengaja (didokumentasikan di README hasil, bukan
disembunyikan):
- Kalau TP dan SL sama-sama tersentuh dalam 1 candle M5 yang sama,
  diasumsikan SL kena duluan (konservatif, bukan optimis).
- Fill price untuk pending order = persis di level entry sinyal (tidak
  ada simulasi slippage).
- Follow-up "harga saat itu" didekati dari candle open pada waktu
  follow-up diposting (bukan tick-level).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backtest.price_data import PriceSeries
from src.trading.mt5_client import compute_market_tolerance, decide_order_kind
from src.trading.risk import calculate_lot, calculate_partial_close_volume


@dataclass
class SymbolSpec:
    broker_symbol: str
    point: float
    digits: int
    trade_tick_size: float
    trade_tick_value: float
    volume_step: float
    volume_min: float
    volume_max: float
    trade_stops_level: float = 0.0


@dataclass
class SimulatedTrade:
    signal_message_id: int
    canonical_symbol: str
    direction: str
    lot: float
    entry_price: float
    entry_time: datetime
    sl: float
    tp: float
    kind: str  # "MARKET" | "BUY_STOP" | "SELL_STOP" | "BUY_LIMIT" | "SELL_LIMIT"

    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None  # "tp" | "sl" | "still_open" | None (belum resolve)
    be_moved: bool = False
    tp1_hit: bool = False
    remaining_lot: float = field(default=0.0)
    realized_pnl_usd: float = 0.0

    _last_resolved_index: int = field(default=-1, repr=False)

    def __post_init__(self):
        self.remaining_lot = self.lot

    @property
    def is_closed(self) -> bool:
        return self.exit_reason in ("tp", "sl")

    @property
    def is_open(self) -> bool:
        return not self.is_closed


def resolve_entry_fill(
    direction: str,
    entry: Optional[float],
    entry_range: Optional[tuple],
    series: PriceSeries,
    signal_time: datetime,
    spec: SymbolSpec,
    max_deviation_pips: float,
):
    """Return (fill_index, fill_price, kind) atau None kalau tidak ada
    data harga di waktu itu, atau (untuk pending order) tidak pernah
    tersentuh sampai akhir data."""
    start_idx = series.index_at_or_after(signal_time)
    if start_idx is None:
        return None

    current_price = series.candles[start_idx].open

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

    # Pending order -- scan maju sampai level target tersentuh.
    for i in range(start_idx, len(series.candles)):
        candle = series.candles[i]
        if kind in ("BUY_STOP", "SELL_LIMIT"):
            # nunggu harga NAIK menyentuh target
            if candle.high >= target:
                return i, target, kind
        else:  # BUY_LIMIT, SELL_STOP -- nunggu harga TURUN menyentuh target
            if candle.low <= target:
                return i, target, kind

    return None  # tidak pernah tersentuh sampai akhir data


def resolve_trade_up_to(trade: SimulatedTrade, series: PriceSeries, up_to_time: datetime) -> None:
    """Majukan status trade sampai up_to_time kalau memang sudah kena
    TP/SL sebelum itu. Dipanggil incremental (lanjut dari candle
    terakhir yang sudah dicek), supaya SL yang berubah di tengah jalan
    (move_sl_be) cuma berlaku untuk candle SETELAH follow-up itu terjadi
    -- bukan diterapkan retroaktif ke masa lalu."""
    if trade.is_closed:
        return

    if trade._last_resolved_index == -1:
        start_idx = series.index_at_or_after(trade.entry_time)
        if start_idx is None:
            trade.exit_reason = "still_open"  # tidak ada data sama sekali
            return
    else:
        start_idx = trade._last_resolved_index + 1

    is_buy = trade.direction == "BUY"

    for i in range(start_idx, len(series.candles)):
        candle = series.candles[i]
        if candle.time > up_to_time:
            trade._last_resolved_index = i - 1
            return

        sl_hit = (candle.low <= trade.sl) if is_buy else (candle.high >= trade.sl)
        tp_hit = (candle.high >= trade.tp) if is_buy else (candle.low <= trade.tp)

        if sl_hit:
            # SL dicek duluan drpd TP kalau dua-duanya kena di candle yang
            # sama -- asumsi konservatif yang disengaja (lihat docstring modul).
            trade.exit_price = trade.sl
            trade.exit_time = candle.time
            trade.exit_reason = "sl"
            trade._last_resolved_index = i
            return
        if tp_hit:
            trade.exit_price = trade.tp
            trade.exit_time = candle.time
            trade.exit_reason = "tp"
            trade._last_resolved_index = i
            return

    trade._last_resolved_index = len(series.candles) - 1


def pnl_usd(trade: SimulatedTrade, exit_price: float, lot: float, spec: SymbolSpec) -> float:
    """P/L dalam dolar untuk sebagian/seluruh lot yang closed di exit_price.

    tick_value dari broker sudah didefinisikan PER 1.0 LOT (konvensi MT5
    standar) -- jadi P/L tinggal (jarak harga / tick_size) * tick_value * lot,
    TIDAK perlu dibagi/dikali volume_step (itu cuma granularity lot, tidak
    ada hubungannya dengan perhitungan P/L)."""
    is_buy = trade.direction == "BUY"
    price_diff = (exit_price - trade.entry_price) if is_buy else (trade.entry_price - exit_price)
    ticks = price_diff / spec.trade_tick_size
    return ticks * spec.trade_tick_value * lot
