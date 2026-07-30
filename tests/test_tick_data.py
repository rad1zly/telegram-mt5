import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from backtest.tick_data import Tick, TickSeries  # noqa: E402


def test_from_csv_parses_standard_mt5_tick_export(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
        "2025.03.03\t04:15:00.123\t4344.10\t4344.30\t0\t0\t6\n"
        "2025.03.03\t04:15:01.500\t4344.20\t4344.40\t0\t0\t6\n"
    )
    series = TickSeries.from_csv(str(path))

    assert len(series) == 2
    assert series.ticks[0].bid == 4344.10
    assert series.ticks[0].ask == 4344.30
    assert series.ticks[0].time == datetime(2025, 3, 3, 4, 15, 0, 123000, tzinfo=timezone.utc)
    assert series.ticks[1].time > series.ticks[0].time


def test_from_csv_handles_time_without_milliseconds(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\n"
        "2025.03.03\t04:15:00\t4344.10\t4344.30\n"
    )
    series = TickSeries.from_csv(str(path))
    assert len(series) == 1
    assert series.ticks[0].time == datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc)


def test_from_csv_skips_malformed_rows(tmp_path):
    path = tmp_path / "ticks.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\n"
        "2025.03.03\t04:15:00\t4344.10\t4344.30\n"
        "2025.03.03\t04:15:01\t\t4344.40\n"  # bid kosong -> dilewati
        "garbage line tanpa kolom cukup\n"
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
    ticks = [
        Tick(time=datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc), bid=100.0, ask=100.1),
        Tick(time=datetime(2025, 3, 3, 4, 15, 5, tzinfo=timezone.utc), bid=100.2, ask=100.3),
        Tick(time=datetime(2025, 3, 3, 4, 15, 10, tzinfo=timezone.utc), bid=100.4, ask=100.5),
    ]
    series = TickSeries(ticks)

    assert series.index_at_or_after(datetime(2025, 3, 3, 4, 15, 3, tzinfo=timezone.utc)) == 1
    assert series.index_at_or_after(t0) == 0


def test_index_at_or_after_returns_none_before_data_starts():
    ticks = [Tick(time=datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc), bid=100.0, ask=100.1)]
    series = TickSeries(ticks)
    assert series.index_at_or_after(datetime(2025, 1, 1, tzinfo=timezone.utc)) is None


def test_index_at_or_after_returns_none_past_data_end():
    ticks = [Tick(time=datetime(2025, 3, 3, 4, 15, 0, tzinfo=timezone.utc), bid=100.0, ask=100.1)]
    series = TickSeries(ticks)
    assert series.index_at_or_after(datetime(2026, 1, 1, tzinfo=timezone.utc)) is None
