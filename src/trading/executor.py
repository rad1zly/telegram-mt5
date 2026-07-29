"""Orkestrasi: Signal terparsing -> resolve simbol broker -> hitung lot
dari risiko tetap -> kirim order ke MT5.

Ini dipakai tools/execute_test_signal.py untuk uji coba manual satu-satu
ke akun demo. Guard penuh (spread, max trade/hari, daily loss cap — lihat
plan Fase 3) belum semua ada di sini; itu menyusul saat pipeline live
24/7 dibangun. Untuk sekarang, guard yang sudah aktif: simbol harus
ke-resolve jelas, SL harus ada, dan lot harus lolos validasi volume_min
broker.
"""

from dataclasses import dataclass
from typing import Optional

from src.parser.schema import Signal
from src.trading import mt5_client
from src.trading.risk import calculate_lot
from src.trading.symbols import SymbolResolver


@dataclass
class ExecutionResult:
    success: bool
    detail: str
    ticket: Optional[int] = None
    lot: Optional[float] = None
    price: Optional[float] = None
    broker_symbol: Optional[str] = None


def _resolve_effective_entry(signal: Signal, current_price: float) -> float:
    """Kalau signal.entry tunggal, pakai apa adanya. Kalau entry berupa
    rentang (mis. 4020-4025):

    - harga sekarang SUDAH di dalam rentang -> pakai harga sekarang
      (market order, tidak perlu tunggu — kita memang sudah di zona itu);
    - harga sekarang di LUAR rentang -> pakai sisi rentang yang TERDEKAT
      dari harga sekarang (bukan titik tengah), supaya order/pending yang
      dipasang punya peluang fill paling masuk akal dan risiko (jarak ke
      SL) terhitung akurat sesuai posisi harga sungguhan.

    Kalau signal.entry DAN signal.entry_range dua-duanya None (channel
    cuma bilang "Sell Now" tanpa level spesifik) -> market di harga
    sekarang, tidak ada yang perlu diresolve.
    """
    if signal.entry is not None:
        return signal.entry

    if signal.entry_range is None:
        return current_price

    low, high = signal.entry_range
    if low <= current_price <= high:
        return current_price
    if current_price < low:
        return low
    return high


def execute_signal(
    signal: Signal,
    resolver: SymbolResolver,
    broker_symbols: list[str],
    risk_usd: float,
    max_lot_cap: float,
    max_price_deviation_pips: float = 15.0,
    price_deviation_overrides: Optional[dict] = None,
    min_sl_distance_overrides: Optional[dict] = None,
) -> ExecutionResult:
    """price_deviation_overrides: {canonical_symbol: pips} — satu angka
    'pips' global TIDAK bisa cocok untuk semua instrumen sekaligus (mis.
    gold kuotasi 2 desimal: 15 pip cuma $0.15, jauh lebih kecil dari gap
    harga wajar yang sering terjadi). Override per simbol menang atas
    max_price_deviation_pips global kalau canonical symbol-nya ada di sini.

    min_sl_distance_overrides: {canonical_symbol: jarak minimum dalam
    satuan harga} — pengaman terhadap SL yang salah baca/salah ketik
    (mis. channel nulis SL yang jaraknya cuma 1 poin dari entry padahal
    biasanya puluhan poin). Diturunkan dari median jarak SL riwayat
    channel per simbol, bukan angka global — skala harga tiap instrumen
    beda jauh (GOLD vs indeks vs forex 4 digit)."""
    if signal.sl is None:
        return ExecutionResult(success=False, detail="Signal tidak punya SL — ditolak, tidak bisa hitung risiko")

    resolved = resolver.resolve(signal.symbol, broker_symbols)
    if not resolved.ok:
        return ExecutionResult(success=False, detail=f"Simbol ditolak: {resolved.error}")
    broker_symbol = resolved.matched

    effective_deviation_pips = (price_deviation_overrides or {}).get(
        resolved.canonical, max_price_deviation_pips
    )

    info = mt5_client.get_symbol_info(broker_symbol)
    if info is None:
        return ExecutionResult(success=False, detail=f"symbol_info kosong untuk {broker_symbol}")

    current_price = mt5_client.get_current_price(broker_symbol, signal.action)
    if current_price is None:
        return ExecutionResult(success=False, detail=f"Tidak bisa ambil harga live untuk {broker_symbol}")

    entry = _resolve_effective_entry(signal, current_price)

    min_sl_distance = (min_sl_distance_overrides or {}).get(resolved.canonical, 0.0)

    lot_result = calculate_lot(
        entry=entry,
        sl=signal.sl,
        tick_size=info.trade_tick_size,
        tick_value=info.trade_tick_value,
        volume_step=info.volume_step,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        risk_usd=risk_usd,
        max_lot_cap=max_lot_cap,
        min_sl_distance=min_sl_distance,
    )
    if not lot_result.ok:
        return ExecutionResult(success=False, detail=f"Lot ditolak: {lot_result.error}")

    tp = signal.tp[-1] if signal.tp else 0.0

    order_result = mt5_client.send_order(
        symbol=broker_symbol,
        direction=signal.action,
        volume=lot_result.lot,
        entry=entry,
        sl=signal.sl,
        tp=tp,
        comment=f"tg-signal-{signal.message_id}",
        max_deviation_pips=effective_deviation_pips,
    )
    if not order_result.success:
        return ExecutionResult(success=False, detail=f"Order gagal: {order_result.error}")

    capped_note = " (lot dipotong max_lot_cap)" if lot_result.capped else ""
    return ExecutionResult(
        success=True,
        detail=(
            f"Order sukses: {order_result.kind} {signal.action} {broker_symbol} "
            f"lot={lot_result.lot}{capped_note} @ {order_result.price}"
        ),
        ticket=order_result.ticket,
        lot=lot_result.lot,
        price=order_result.price,
        broker_symbol=broker_symbol,
    )
