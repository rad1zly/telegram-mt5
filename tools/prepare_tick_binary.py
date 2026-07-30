"""Convert file tick CSV BESAR (bisa puluhan GB, ratusan juta baris) jadi
3 file biner mentah (times/bids/asks) di disk -- DIPROSES BERTAHAP (chunked)
supaya RAM yang dipakai TETAP KECIL sepanjang proses, tidak peduli seberapa
besar file CSV aslinya. Solusi utk PC dengan RAM terbatas (mis. 6GB) yang
tidak sanggup muat ratusan juta baris CSV sekaligus.

    .venv\\Scripts\\python.exe tools\\prepare_tick_binary.py "D:\\XAUUSD+_..._....csv" backtest\\data\\ticks_bin\\XAUUSD

Ulangi utk keempat simbol (XAUUSD, NAS100, US30, SP500) dgn prefix output
beda-beda. Proses ini SEKALI SAJA per file -- hasilnya dipakai berulang-
ulang oleh tools/run_backtest_ticks.py tanpa perlu convert ulang.

Hasil: <prefix>.times.bin, <prefix>.bids.bin, <prefix>.asks.bin -- dibaca
lewat TickSeries.from_binary() pakai numpy.memmap (OS cuma nge-load bagian
yang benar-benar diakses ke RAM saat backtest jalan, bukan seluruh file).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

CHUNK_SIZE = 5_000_000  # baris per chunk -- sekitar 150-200MB RAM per chunk, aman utk RAM kecil


def convert(csv_path: str, out_prefix: str, chunk_size: int = CHUNK_SIZE) -> int:
    with open(csv_path) as f:
        header = f.readline()
    delimiter = "\t" if "\t" in header else ","

    out_dir = os.path.dirname(out_prefix)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    times_path = out_prefix + ".times.bin"
    bids_path = out_prefix + ".bids.bin"
    asks_path = out_prefix + ".asks.bin"

    total_rows = 0
    with open(times_path, "wb") as ft, open(bids_path, "wb") as fb, open(asks_path, "wb") as fa:
        reader = pd.read_csv(
            csv_path, sep=delimiter, dtype=str, engine="c", header=0,
            on_bad_lines="skip", keep_default_na=False, chunksize=chunk_size,
        )
        for chunk_num, df in enumerate(reader, start=1):
            df.columns = [c.strip().strip("<>").upper() for c in df.columns]
            required = ("DATE", "TIME", "BID", "ASK")
            missing = [c for c in required if c not in df.columns]
            if missing:
                raise ValueError(
                    f"Header tick CSV tidak dikenali, kolom hilang {missing} "
                    f"(kolom yang ada: {list(df.columns)})"
                )

            # Waktu tick MT5 biasanya HH:MM:SS.mmm (milidetik) tapi kadang
            # tanpa itu -- normalisasi dulu SEBELUM parse sekali dgn format
            # tetap (vectorized, bukan coba-gagal per baris).
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
            bids = bid[valid].to_numpy(dtype="float32")
            asks = ask[valid].to_numpy(dtype="float32")

            # Urutkan DALAM chunk (jaga-jaga) -- TIDAK sort ulang seluruh
            # file (itu butuh semua data resident, bertentangan dgn tujuan
            # bertahap ini). Export MT5 normalnya sudah kronologis per file,
            # jadi antar-chunk seharusnya tetap terurut tanpa perlu ini.
            order = np.argsort(times_ns, kind="stable")
            times_ns[order].tofile(ft)
            bids[order].tofile(fb)
            asks[order].tofile(fa)

            total_rows += len(times_ns)
            print(f"  chunk {chunk_num}: {len(times_ns):,} baris valid (total sejauh ini: {total_rows:,})")

    return total_rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_path", help="Path file tick CSV mentah (hasil export MT5 -> Export Ticks)")
    p.add_argument("out_prefix", help="Prefix file biner output, mis. backtest/data/ticks_bin/XAUUSD")
    p.add_argument("--chunk-size", type=int, default=CHUNK_SIZE, help="Baris per chunk (lebih kecil = lebih hemat RAM, lebih lambat)")
    args = p.parse_args()

    if not os.path.exists(args.csv_path):
        print(f"[!] File tidak ada: {args.csv_path}")
        return

    print(f"Mengonversi {args.csv_path} -> {args.out_prefix}.{{times,bids,asks}}.bin ...")
    total = convert(args.csv_path, args.out_prefix, args.chunk_size)
    print(f"\nSELESAI. {total:,} tick tersimpan.")
    print(f"  {args.out_prefix}.times.bin")
    print(f"  {args.out_prefix}.bids.bin")
    print(f"  {args.out_prefix}.asks.bin")
    print(f"\nPakai di run_backtest_ticks.py dgn menunjuk prefix ini (bukan nama .csv).")


if __name__ == "__main__":
    main()
