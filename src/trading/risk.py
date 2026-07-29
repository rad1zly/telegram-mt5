"""Perhitungan lot dari risiko dolar tetap (usd_per_trade), BUKAN dari
persentase % di teks signal — channel sering cantumkan 'risk 1%' tapi
keputusan project ini sudah dikunci: risiko tetap per trade, apapun kata
channel. tick_size/tick_value/volume_step/min/max WAJIB diambil live dari
broker (mt5.symbol_info()), tidak pernah di-hardcode di sini.
"""

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class LotSizingResult:
    lot: Optional[float]
    capped: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.lot is not None


def calculate_lot(
    entry: float,
    sl: float,
    tick_size: float,
    tick_value: float,
    volume_step: float,
    volume_min: float,
    volume_max: float,
    risk_usd: float,
    max_lot_cap: float,
) -> LotSizingResult:
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return LotSizingResult(lot=None, error="Jarak SL ke entry nol/negatif — signal tidak valid")
    if tick_size <= 0 or tick_value <= 0:
        return LotSizingResult(lot=None, error="tick_size/tick_value dari broker tidak valid (<=0)")

    loss_per_lot = (sl_distance / tick_size) * tick_value
    if loss_per_lot <= 0:
        return LotSizingResult(lot=None, error="Perhitungan loss_per_lot tidak valid (<=0)")

    lot_raw = risk_usd / loss_per_lot
    lot = round(math.floor(lot_raw / volume_step) * volume_step, 8)

    capped = False
    effective_max = min(volume_max, max_lot_cap)
    if lot > effective_max:
        lot = round(math.floor(effective_max / volume_step) * volume_step, 8)
        capped = True

    if lot < volume_min:
        return LotSizingResult(
            lot=None,
            error=(
                f"Lot hasil hitungan ({lot}) di bawah volume_min broker ({volume_min}) — "
                f"risiko ${risk_usd} terlalu kecil untuk jarak SL ini. Ditolak, bukan dibulatkan naik."
            ),
        )

    return LotSizingResult(lot=lot, capped=capped)


@dataclass
class PartialCloseResult:
    action: str  # "partial" | "full" | "reject"
    volume: Optional[float] = None  # volume yang ditutup (None kalau action="full" -> tutup semua)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.action in ("partial", "full")


def calculate_partial_close_volume(
    position_lot: float,
    percent: float,
    volume_step: float,
    volume_min: float,
) -> PartialCloseResult:
    """Hitung volume partial-close, dibulatkan KE BAWAH ke volume_step
    broker (bukan round() generik 2 desimal — index/beberapa broker punya
    step 0.1 atau 1.0, bukan 0.01).

    Kalau target volume di bawah volume_min (mis. lot 1.0 dengan step 1.0,
    50% = 0.5 yang tidak valid), ATAU sisa setelah partial close di bawah
    volume_min (posisi kecil, sisa jadi tidak valid) -> tutup PENUH saja,
    bukan kirim angka yang pasti ditolak broker.
    """
    if position_lot <= 0:
        return PartialCloseResult(action="reject", error="Lot posisi tidak valid (<=0)")

    target = position_lot * (percent / 100)
    close_volume = round(math.floor(target / volume_step) * volume_step, 8)

    if close_volume < volume_min:
        return PartialCloseResult(action="full")

    remainder = round(position_lot - close_volume, 8)
    if remainder < volume_min:
        return PartialCloseResult(action="full")

    return PartialCloseResult(action="partial", volume=close_volume)
