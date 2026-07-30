"""Timestamp di file export MT5 = WAKTU SERVER BROKER (mayoritas GMT+3),
sedangkan timestamp pesan Telegram = UTC ASLI. Kalau tidak dikoreksi,
SELURUH backtest bergeser berjam-jam (sinyal dicocokkan ke candle yang
salah). Test ini mengunci perilaku koreksinya di M5 maupun tick.
"""

import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from backtest.price_data import PriceSeries  # noqa: E402
from backtest.tick_data import TickSeries, _to_ns  # noqa: E402


def _write_m5(tmp_path):
    path = tmp_path / "m5.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n"
        "2025.03.03\t12:00:00\t4344.0\t4345.0\t4343.0\t4344.5\n"
        "2025.03.03\t12:05:00\t4344.5\t4346.0\t4344.0\t4345.5\n"
    )
    return str(path)


def test_price_series_without_offset_keeps_file_timestamp_as_is(tmp_path):
    series = PriceSeries.from_csv(_write_m5(tmp_path), server_utc_offset_hours=0.0)
    assert series.candles[0].time == datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc)


def test_price_series_shifts_server_time_back_to_real_utc(tmp_path):
    # Candle berlabel 12:00 di server GMT+3 sebenarnya terjadi 09:00 UTC.
    series = PriceSeries.from_csv(_write_m5(tmp_path), server_utc_offset_hours=3.0)
    assert series.candles[0].time == datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc)
    assert series.candles[1].time == datetime(2025, 3, 3, 9, 5, tzinfo=timezone.utc)


def test_price_series_lookup_uses_corrected_time(tmp_path):
    # Pesan Telegram jam 09:00 UTC harus ketemu candle berlabel 12:00 server.
    series = PriceSeries.from_csv(_write_m5(tmp_path), server_utc_offset_hours=3.0)
    idx = series.index_at_or_after(datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc))
    assert idx == 0
    # ...dan jam 12:00 UTC sudah MELEWATI akhir data (data cuma s/d 09:05 UTC)
    assert series.index_at_or_after(datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc)) is None


def _tick_series(offset_hours):
    # dua tick berlabel waktu server 12:00:00 dan 12:00:05
    base = datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc)
    times = np.array([_to_ns(base), _to_ns(base) + 5_000_000_000], dtype="int64")
    bids = np.array([4344.0, 4344.2], dtype="float64")
    asks = np.array([4344.2, 4344.4], dtype="float64")
    return TickSeries(times=times, bids=bids, asks=asks, server_utc_offset_hours=offset_hours)


def test_tick_series_time_at_returns_real_utc():
    series = _tick_series(3.0)
    assert series.time_at(0) == datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc)


def test_tick_series_lookup_shifts_query_not_the_array():
    series = _tick_series(3.0)
    # Query pakai UTC asli (09:00) -> ketemu tick berlabel server 12:00.
    assert series.index_at_or_after(datetime(2025, 3, 3, 9, 0, tzinfo=timezone.utc)) == 0
    # Array mentahnya TIDAK ikut digeser -- penting supaya memmap tetap
    # utuh (menggeser ratusan juta elemen akan memaksa semuanya ke RAM).
    assert series.times[0] == _to_ns(datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc))


def test_tick_series_without_offset_is_unchanged():
    series = _tick_series(0.0)
    assert series.time_at(0) == datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc)
    assert series.index_at_or_after(datetime(2025, 3, 3, 12, 0, tzinfo=timezone.utc)) == 0


def test_tick_and_price_series_agree_on_the_same_instant(tmp_path):
    """M5 dan tick harus memetakan waktu server yang sama ke UTC yang sama
    -- kalau tidak, backtest candle dan backtest tick akan diam-diam
    memakai kerangka waktu berbeda dan hasilnya tidak bisa dibandingkan."""
    price = PriceSeries.from_csv(_write_m5(tmp_path), server_utc_offset_hours=3.0)
    ticks = _tick_series(3.0)
    assert price.candles[0].time == ticks.time_at(0)
