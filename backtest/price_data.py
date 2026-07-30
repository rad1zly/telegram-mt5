"""Loader untuk candle M5 hasil export MT5 (format History Center: tab-
separated, header <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL>
<VOL> <SPREAD>).

Menyediakan lookup cepat "candle pertama pada/setelah waktu X" dan
forward-walk dari titik itu — dipakai simulasi entry fill dan resolusi
TP/SL di engine.py.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float


class PriceSeries:
    def __init__(self, candles: list[Candle]):
        # harus terurut ascending by time -- dijamin oleh from_csv (data MT5
        # export sudah kronologis) dan oleh pemanggil test yang membuat manual.
        self.candles = candles
        self._times = [c.time for c in candles]

    @classmethod
    def from_csv(cls, path: str) -> "PriceSeries":
        candles = []
        with open(path) as f:
            header = f.readline()
            delimiter = "\t" if "\t" in header else ","
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split(delimiter)
                if len(parts) < 6:
                    continue
                date_str, time_str, o, h, low_, c = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
                candles.append(Candle(time=dt, open=float(o), high=float(h), low=float(low_), close=float(c)))
        return cls(candles)

    def index_at_or_after(self, dt: datetime) -> Optional[int]:
        """Index candle pertama dengan time >= dt, atau None kalau dt
        melewati akhir data yang tersedia ATAU dt sebelum data yang
        tersedia mulai sama sekali (mis. sinyal Januari 2025 sementara
        data M5 baru mulai Maret 2025) -- tanpa guard ini, bisect_left
        diam-diam mengembalikan index 0 (candle pertama yg ada), membuat
        "harga saat itu" untuk sinyal lama terisi dengan harga BERMINGGU-
        MINGGU kemudian, mencemari simulasi entry-fill/TP/SL-nya."""
        if not self.candles or dt < self.candles[0].time:
            return None
        idx = bisect_left(self._times, dt)
        if idx >= len(self.candles):
            return None
        return idx

    def __len__(self) -> int:
        return len(self.candles)
