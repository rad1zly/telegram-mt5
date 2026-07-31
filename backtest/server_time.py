"""Konversi WAKTU SERVER BROKER <-> UTC ASLI.

Timestamp di file export MT5 (M5 maupun tick) adalah waktu server broker,
sedangkan timestamp pesan Telegram adalah UTC asli. Tanpa konversi, seluruh
backtest bergeser berjam-jam dan hasilnya tidak ada artinya.

KENAPA TIDAK CUKUP OFFSET TETAP: mayoritas broker MT5 memakai EET/EEST --
UTC+2 di musim dingin, UTC+3 di musim panas, ikut aturan DST Eropa. Offset
tetap +3 benar untuk Maret-Oktober tapi MELESET SATU JAM sepanjang
November-Februari. Terukur jelas di korpus: akurasi verifikasi klaim
channel anjlok ke 29-50% di Januari-Februari sementara Maret-Juli 76-98%,
dan pengukuran offset per bulan memang menunjukkan +2h di musim dingin.

Dua cara pakai, keduanya didukung config:
- server_timezone: "Europe/Athens"  -> DST otomatis (DIREKOMENDASIKAN)
- server_utc_offset_hours: 3.0      -> offset tetap (broker tanpa DST,
  atau kalau tzdata tidak tersedia di mesin itu)

Ukur/verifikasi punya broker sendiri dengan:
    .venv/bin/python tools/detect_server_timezone.py
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


class ServerClock:
    """Penerjemah dua arah antara waktu server broker dan UTC asli.

    Dibuat lewat from_config() supaya semua pemanggil (M5, tick, tools)
    membaca aturan yang sama persis dari config -- kalau tidak, backtest
    candle dan backtest tick bisa diam-diam memakai kerangka waktu berbeda
    dan hasilnya tidak bisa dibandingkan.
    """

    def __init__(self, tz=None, extra_hours: float = 0.0, fixed_offset_hours: float = 0.0):
        """tz + extra_hours: waktu server = waktu di zona `tz`, DITAMBAH
        extra_hours. Terdengar berputar, tapi inilah cara memodelkan broker
        yang basisnya UTC+2 tapi ikut aturan DST AMERIKA: pakai
        tz=America/New_York (UTC-5 dingin / UTC-4 panas) lalu +7 jam,
        hasilnya persis UTC+2 dingin / UTC+3 panas dengan tanggal peralihan
        mengikuti New York. Aturan DST-nya diambil dari database tz sistem,
        jadi tidak perlu di-hardcode dan tetap benar tahun-tahun berikutnya.

        fixed_offset_hours dipakai hanya kalau tz=None (broker tanpa DST)."""
        self._tz = tz
        self._extra = timedelta(hours=extra_hours)
        self._fixed = timedelta(hours=fixed_offset_hours)

    @classmethod
    def from_config(cls, backtest_settings: Optional[dict]) -> "ServerClock":
        settings = backtest_settings or {}
        tz_name = settings.get("server_timezone")
        if tz_name:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 -- opsional, cuma perlu kalau dipakai

            return cls(tz=ZoneInfo(tz_name), extra_hours=settings.get("server_timezone_extra_hours", 0.0))
        return cls(fixed_offset_hours=settings.get("server_utc_offset_hours", 0.0))

    @property
    def is_dst_aware(self) -> bool:
        return self._tz is not None

    def describe(self) -> str:
        if self._tz is not None:
            extra = self._extra.total_seconds() / 3600
            suffix = f" {extra:+g}h" if extra else ""
            return f"{self._tz.key}{suffix} (DST otomatis)"
        hours = self._fixed.total_seconds() / 3600
        return f"UTC{hours:+g}h (offset tetap)"

    def to_utc(self, server_naive: datetime) -> datetime:
        """Waktu server (naive, apa adanya dari file) -> UTC asli."""
        if self._tz is not None:
            local_naive = server_naive - self._extra
            return local_naive.replace(tzinfo=self._tz).astimezone(timezone.utc)
        return server_naive.replace(tzinfo=timezone.utc) - self._fixed

    def to_server(self, utc_time: datetime) -> datetime:
        """UTC asli -> waktu server (naive, siap dibandingkan dengan isi file).

        Kebalikan tepat dari to_utc: server = UTC + offset (broker UTC+3
        berarti jam server 3 jam LEBIH MAJU dari UTC)."""
        if self._tz is not None:
            return utc_time.astimezone(self._tz).replace(tzinfo=None) + self._extra
        return (utc_time + self._fixed).replace(tzinfo=None)
