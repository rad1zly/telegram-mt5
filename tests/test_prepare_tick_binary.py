import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from backtest.tick_data import TickSeries  # noqa: E402
from tools.prepare_tick_binary import convert  # noqa: E402


def test_convert_produces_readable_binary_matching_csv_content(tmp_path):
    csv_path = tmp_path / "raw_ticks.csv"
    csv_path.write_text(
        "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
        "2025.03.03\t04:15:00.123\t4344.10\t4344.30\t0\t0\t6\n"
        "2025.03.03\t04:15:01.500\t4344.20\t4344.40\t0\t0\t6\n"
        "2025.03.03\t04:15:02.000\t\t4344.50\t0\t0\t6\n"  # bid kosong -> dilewati
    )
    out_prefix = str(tmp_path / "out" / "XAUUSD")

    total = convert(str(csv_path), out_prefix, chunk_size=1)  # chunk_size=1 -- paksa multi-chunk

    assert total == 2
    series = TickSeries.from_binary(out_prefix)
    assert len(series) == 2
    assert abs(series.bids[0] - 4344.10) < 1e-4
    assert abs(series.asks[1] - 4344.40) < 1e-4
    assert series.time_at(0) == datetime(2025, 3, 3, 4, 15, 0, 123000, tzinfo=timezone.utc)


def test_convert_raises_on_unrecognized_header(tmp_path):
    csv_path = tmp_path / "raw.csv"
    csv_path.write_text("<FOO>\t<BAR>\n1\t2\n")
    try:
        convert(str(csv_path), str(tmp_path / "out" / "X"))
        assert False, "harusnya raise ValueError"
    except ValueError:
        pass
