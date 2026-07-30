"""Loader untuk data TICK hasil export MT5 (History Center -> Export Ticks),
format standar: tab-separated, header <DATE> <TIME> <BID> <ASK> <LAST>
<VOLUME> <FLAGS> (kolom <LAST>/<VOLUME>/<FLAGS> diabaikan kalau tidak
dipakai). Beda dari price_data.py (candle M5): di sini tiap baris adalah
SATU harga pada satu waktu presisi, bukan agregat open/high/low/close per
interval -- makanya deteksi SL/TP tidak butuh asumsi "kalau dua-duanya
kena di candle yang sama, SL duluan" lagi (lihat tick_engine.py).
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: float
    ask: float


class TickSeries:
    def __init__(self, ticks: list[Tick]):
        # harus terurut ascending by time -- dijamin oleh from_csv (export
        # MT5 sudah kronologis) dan oleh pemanggil test yang membuat manual.
        self.ticks = ticks
        self._times = [t.time for t in ticks]

    @classmethod
    def from_csv(cls, path: str) -> "TickSeries":
        ticks = []
        with open(path) as f:
            header = f.readline()
            delimiter = "\t" if "\t" in header else ","
            header_cols = [c.strip().strip("<>").upper() for c in header.split(delimiter)]
            try:
                date_i = header_cols.index("DATE")
                time_i = header_cols.index("TIME")
                bid_i = header_cols.index("BID")
                ask_i = header_cols.index("ASK")
            except ValueError as e:
                raise ValueError(
                    f"Header tick CSV tidak dikenali (butuh kolom DATE/TIME/BID/ASK): {header!r}"
                ) from e

            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split(delimiter)
                if len(parts) <= max(date_i, time_i, bid_i, ask_i):
                    continue
                date_str, time_str = parts[date_i], parts[time_i]
                bid_str, ask_str = parts[bid_i], parts[ask_i]
                if not bid_str or not ask_str:
                    continue
                # waktu tick MT5 biasanya HH:MM:SS.mmm (milidetik) -- coba
                # dua format, jatuh ke yang tanpa milidetik kalau gagal.
                dt = None
                for fmt in ("%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(f"{date_str} {time_str}", fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if dt is None:
                    continue
                try:
                    bid, ask = float(bid_str), float(ask_str)
                except ValueError:
                    continue
                if bid <= 0 or ask <= 0:
                    continue
                ticks.append(Tick(time=dt, bid=bid, ask=ask))
        return cls(ticks)

    def index_at_or_after(self, dt: datetime) -> Optional[int]:
        """Index tick pertama dengan time >= dt, atau None kalau dt
        melewati akhir data ATAU dt sebelum data mulai sama sekali (lihat
        alasan yang sama di price_data.py:index_at_or_after)."""
        if not self.ticks or dt < self.ticks[0].time:
            return None
        idx = bisect_left(self._times, dt)
        if idx >= len(self.ticks):
            return None
        return idx

    def __len__(self) -> int:
        return len(self.ticks)
