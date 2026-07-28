"""Orkestrasi: Signal terparsing -> resolve simbol broker -> hitung lot
dari risiko tetap -> kirim order ke MT5.

Ini dipakai tools/execute_test_signal.py untuk uji coba manual satu-satu
ke akun demo. Guard penuh (spread, deviasi harga, max trade/hari, daily
loss cap — lihat plan Fase 3) belum semua ada di sini; itu menyusul saat
pipeline live 24/7 dibangun. Untuk sekarang, guard yang sudah aktif:
simbol harus ke-resolve jelas, SL harus ada, dan lot harus lolos validasi
volume_min broker.
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


def execute_signal(
    signal: Signal,
    resolver: SymbolResolver,
    broker_symbols: list[str],
    risk_usd: float,
    max_lot_cap: float,
) -> ExecutionResult:
    if signal.sl is None:
        return ExecutionResult(success=False, detail="Signal tidak punya SL — ditolak, tidak bisa hitung risiko")

    resolved = resolver.resolve(signal.symbol, broker_symbols)
    if not resolved.ok:
        return ExecutionResult(success=False, detail=f"Simbol ditolak: {resolved.error}")
    broker_symbol = resolved.matched

    info = mt5_client.get_symbol_info(broker_symbol)
    if info is None:
        return ExecutionResult(success=False, detail=f"symbol_info kosong untuk {broker_symbol}")

    entry = signal.entry if signal.entry is not None else sum(signal.entry_range) / 2

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
