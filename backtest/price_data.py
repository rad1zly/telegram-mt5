"""Loader untuk candle M5 hasil export MT5 (format History Center: tab-
separated, header <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL>
<VOL> <SPREAD>).

Menyediakan lookup cepat "candle pertama pada/setelah waktu X" dan
forward-walk dari titik itu — dipakai simulasi entry fill dan resolusi
TP/SL di engine.py.

PENTING soal ZONA WAKTU: timestamp di file export MT5 adalah WAKTU SERVER
BROKER, BUKAN UTC. Mayoritas broker MT5 pakai GMT+2/GMT+3. Sementara
timestamp pesan Telegram (date_utc di korpus sinyal) adalah UTC ASLI.
Kalau keduanya diperlakukan sama tanpa koreksi, SELURUH backtest bergeser
beberapa jam — sinyal dicocokkan ke candle yang salah, dan hasil TP/SL
jadi tidak ada artinya.

Offset yang benar diverifikasi EMPIRIS dari data (bukan diasumsikan):
lihat backtest.server_utc_offset_hours di config/settings.yaml dan
tools/detect_server_timezone.py yang mengukurnya dari korpus.
from_csv() menerima offset itu dan mengonversi semua timestamp ke UTC
ASLI saat load, jadi seluruh kode di hilir cukup bekerja dalam UTC.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    def from_csv(cls, path: str, server_utc_offset_hours: float = 0.0,
                 clock=None) -> "PriceSeries":
        """Timestamp di file dikonversi dari waktu server broker ke UTC
        ASLI supaya bisa dibandingkan langsung dengan date_utc pesan
        Telegram (lihat docstring modul).

        clock: ServerClock (backtest/server_time.py) -- CARA YANG DISARANKAN,
        karena paham DST (broker EET/EEST: UTC+2 musim dingin, UTC+3 musim
        panas). server_utc_offset_hours cuma dipakai kalau clock=None,
        untuk broker tanpa DST atau pemanggil lama."""
        if clock is None:
            from backtest.server_time import ServerClock

            clock = ServerClock(fixed_offset_hours=server_utc_offset_hours)
        candles = []
        with open(path, encoding="utf-8") as f:
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
                server_naive = datetime.strptime(f"{date_str} {time_str}", "%Y.%m.%d %H:%M:%S")
                dt = clock.to_utc(server_naive)
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
