"""Loader untuk data TICK hasil export MT5 (History Center -> Export Ticks),
format standar: tab/comma-separated, header <DATE> <TIME> <BID> <ASK> <LAST>
<VOLUME> <FLAGS>. Beda dari price_data.py (candle M5): tiap baris adalah
SATU harga pada satu waktu presisi, bukan agregat open/high/low/close per
interval -- makanya deteksi SL/TP tidak butuh asumsi "kalau dua-duanya kena
di candle yang sama, SL duluan" lagi (lihat tick_engine.py).

PENTING soal skala data: file tick riil (1-2 tahun, instrumen aktif) bisa
berisi RATUSAN JUTA baris (puluhan GB) -- jauh lebih besar dari candle M5.
Simpan sebagai list objek Python per-tick TIDAK PRAKTIS (overhead memori
~10x lipat lebih besar dari data mentahnya, dan parsing baris-per-baris
pakai datetime.strptime bisa makan waktu berjam-jam).

Dua cara load, pilih sesuai RAM yang tersedia:
- from_csv(path): pandas (parsing CSV di C, cepat) -> array numpy TAPI
  SELURUHNYA resident di RAM. Cocok kalau RAM cukup besar (>= ukuran data
  tick, biasanya perlu headroom 2-3x krn overhead parsing sementara).
- from_binary(prefix): baca file biner hasil tools/prepare_tick_binary.py
  (dikonversi bertahap dari CSV, bounded memory) LEWAT numpy.memmap -- OS
  cuma nge-load bagian yang benar-benar diakses, bukan semuanya sekaligus.
  WAJIB dipakai kalau data tick jauh lebih besar dari RAM (mis. data 15GB+
  di PC dgn RAM 6GB) -- from_csv() akan gagal/sangat lambat di skala itu.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_ns(dt: datetime) -> int:
    """Konversi EKSAK (integer, bukan float) ke nanodetik sejak epoch UTC --
    dt TANPA tzinfo diasumsikan UTC (konsisten dgn seluruh codebase ini)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _from_ns(ns: int) -> datetime:
    # presisi mikrodetik cukup (ticks MT5 biasanya milidetik) -- konversi
    # balik ke datetime buat dipakai SimulatedTrade.entry_time/exit_time dst.
    return _EPOCH + timedelta(microseconds=int(ns) // 1000)


class TickSeries:
    def __init__(self, times: np.ndarray, bids: np.ndarray, asks: np.ndarray):
        # times: int64 nanodetik sejak epoch UTC, HARUS terurut ascending
        # (dijamin oleh from_csv via np.argsort, dan oleh pemanggil test).
        self.times = times
        self.bids = bids
        self.asks = asks

    @classmethod
    def from_csv(cls, path: str) -> "TickSeries":
        import pandas as pd

        with open(path) as f:
            header = f.readline()
        delimiter = "\t" if "\t" in header else ","

        df = pd.read_csv(
            path, sep=delimiter, dtype=str, engine="c", header=0,
            on_bad_lines="skip", keep_default_na=False,
        )
        df.columns = [c.strip().strip("<>").upper() for c in df.columns]

        required = ("DATE", "TIME", "BID", "ASK")
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(
                f"Header tick CSV tidak dikenali, kolom hilang {missing} "
                f"(kolom yang ada: {list(df.columns)})"
            )

        # Waktu tick MT5 biasanya HH:MM:SS.mmm (milidetik) tapi kadang tanpa
        # itu -- normalisasi dulu (tambah '.000' kalau tidak ada titik) SEBELUM
        # parse sekali dgn format tetap, supaya tetap vectorized/cepat (bukan
        # coba-gagal per baris).
        time_col = df["TIME"]
        needs_ms = ~time_col.str.contains(r"\.", regex=True)
        time_col = time_col.where(~needs_ms, time_col + ".000")

        dt = pd.to_datetime(
            df["DATE"] + " " + time_col, format="%Y.%m.%d %H:%M:%S.%f",
            errors="coerce", utc=True,
        )
        bid = pd.to_numeric(df["BID"], errors="coerce")
        ask = pd.to_numeric(df["ASK"], errors="coerce")

        valid = dt.notna() & bid.notna() & ask.notna() & (bid > 0) & (ask > 0)

        times_ns = dt[valid].values.astype("datetime64[ns]").astype("int64")
        bids = bid[valid].to_numpy(dtype="float64")
        asks = ask[valid].to_numpy(dtype="float64")

        order = np.argsort(times_ns, kind="stable")
        return cls(times=times_ns[order], bids=bids[order], asks=asks[order])

    @classmethod
    def from_binary(cls, prefix: str) -> "TickSeries":
        """Baca file biner hasil tools/prepare_tick_binary.py LEWAT
        MEMORY-MAP (numpy.memmap) -- OS cuma nge-load bagian file yang
        BENAR-BENAR diakses ke RAM saat itu, bukan seluruh isi file
        sekaligus. Ini solusi utk data tick yang jauh lebih besar dari RAM
        yang tersedia (mis. data 15GB+ di PC dgn RAM 6GB) -- from_csv()
        MEMBUTUHKAN seluruh data resident di RAM, TIDAK cocok utk skala itu.

        prefix: path tanpa akhiran, mis. 'backtest/data/ticks_bin/XAUUSD'
        (harus ada <prefix>.times.bin, <prefix>.bids.bin, <prefix>.asks.bin)."""
        times = np.memmap(prefix + ".times.bin", dtype="int64", mode="r")
        bids = np.memmap(prefix + ".bids.bin", dtype="float32", mode="r")
        asks = np.memmap(prefix + ".asks.bin", dtype="float32", mode="r")
        return cls(times=times, bids=bids, asks=asks)

    def index_at_or_after(self, dt: datetime) -> Optional[int]:
        """Index tick pertama dengan time >= dt, atau None kalau dt
        melewati akhir data ATAU dt sebelum data mulai sama sekali (lihat
        alasan yang sama di price_data.py:index_at_or_after)."""
        if len(self.times) == 0:
            return None
        ts = _to_ns(dt)
        if ts < self.times[0]:
            return None
        idx = int(np.searchsorted(self.times, ts, side="left"))
        if idx >= len(self.times):
            return None
        return idx

    def time_at(self, idx: int) -> datetime:
        return _from_ns(self.times[idx])

    def __len__(self) -> int:
        return len(self.times)
