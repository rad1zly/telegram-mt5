import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

from backtest.tick_data import TickSeries, _to_ns  # noqa: E402


def _series_from_seconds(rows):
    # rows: list of (seconds_offset_from_t0, bid, ask)
    from datetime import timedelta
    t0 = datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc)
    times = np.array([_to_ns(t0 + timedelta(seconds=s)) for s, _, _ in rows], dtype="int64")
    bids = np.array([b for _, b, _ in rows], dtype="float64")
    asks = np.array([a for _, _, a in rows], dtype="float64")
    return TickSeries(times=times, bids=bids, asks=asks)


def test_from_csv_parses_standard_mt5_tick_export(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
        "2025.03.03\t04:15:00.123\t4344.10\t4344.30\t0\t0\t6\n"
        "2025.03.03\t04:15:01.500\t4344.20\t4344.40\t0\t0\t6\n"
    )
    series = TickSeries.from_csv(str(path))

    assert len(series) == 2
    assert series.bids[0] == 4344.10
    assert series.asks[0] == 4344.30
    assert series.time_at(0) == datetime(2025, 3, 3, 4, 15, 0, 123000, tzinfo=timezone.utc)
    assert series.times[1] > series.times[0]


def test_from_csv_handles_time_without_milliseconds(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\n"
        "2025.03.03\t04:15:00\t4344.10\t4344.30\n"
    )
    series = TickSeries.from_csv(str(path))
    assert len(series) == 1
    assert series.time_at(0) == datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc)


def test_from_csv_skips_malformed_rows(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\n"
        "2025.03.03\t04:15:00\t4344.10\t4344.30\n"
        "2025.03.03\t04:15:01\t\t4344.40\n"  # bid kosong -> dilewati
        "2025.03.03\t04:15:02\t4344.30\t4344.50\n"
    )
    series = TickSeries.from_csv(str(path))
    assert len(series) == 2


def test_from_csv_raises_on_unrecognized_header(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text("<FOO>\t<BAR>\n1\t2\n")
    try:
        TickSeries.from_csv(str(path))
        assert False, "harusnya raise ValueError"
    except ValueError:
        pass


def test_index_at_or_after_finds_correct_tick():
    t0 = datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc)
    series = _series_from_seconds([(0, 100.0, 100.1), (5, 100.2, 100.3), (10, 100.4, 100.5)])

    assert series.index_at_or_after(datetime(2025, 3, 3, 4, 15, 3, tzinfo=timezone.utc)) == 1
    assert series.index_at_or_after(t0) == 0


def test_index_at_or_after_returns_none_before_data_starts():
    series = _series_from_seconds([(0, 100.0, 100.1)])
    assert series.index_at_or_after(datetime(2025, 1, 1, tzinfo=timezone.utc)) is None


def test_index_at_or_after_returns_none_past_data_end():
    series = _series_from_seconds([(0, 100.0, 100.1)])
    assert series.index_at_or_after(datetime(2026, 1, 1, tzinfo=timezone.utc)) is None
