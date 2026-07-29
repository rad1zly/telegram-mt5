"""Jalankan parser SAAT INI terhadap seluruh korpus yang sudah di-dump,
dan laporkan cakupannya: berapa yang berhasil diparse sebagai entry
signal, follow-up, atau tidak dikenali sama sekali.

    .venv\\Scripts\\python.exe tools\\analyze_corpus.py

Sepenuhnya lokal — cuma baca tests/fixtures/signals.jsonl, tidak butuh
koneksi Telegram/MT5. Aman dijalankan berkali-kali setelah parser diupdate,
untuk lihat apakah cakupan membaik.

Output: ringkasan angka di layar + tests/fixtures/unrecognized.jsonl berisi
pesan yang tidak ke-parse, untuk direview manual (dipotong pendek di
layar; file lengkapnya tetap lokal, tidak ikut ke-commit).
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parser.followup import parse_followup_regex  # noqa: E402
from src.parser.patterns import parse_entry_signal  # noqa: E402

FIXTURE_PATH = "tests/fixtures/signals.jsonl"
UNRECOGNIZED_PATH = "tests/fixtures/unrecognized.jsonl"


def load_rows() -> list[dict]:
    if not os.path.exists(FIXTURE_PATH):
        raise SystemExit(f"{FIXTURE_PATH} tidak ada. Jalankan tools/dump_history.py atau collect_signals.py dulu.")
    rows = []
    with open(FIXTURE_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    rows = load_rows()
    entry_ok = []
    followup_ok = []
    unrecognized = []

    for row in rows:
        text = row.get("text") or ""
        if not text.strip():
            continue
        msg_id = row["message_id"]

        signal = parse_entry_signal(text, message_id=msg_id)
        if signal is not None:
            entry_ok.append((row, signal))
            continue

        followup = parse_followup_regex(text, message_id=msg_id, reply_to_msg_id=row.get("reply_to_msg_id"))
        if followup is not None:
            followup_ok.append((row, followup))
            continue

        unrecognized.append(row)

    total = len(rows)
    print(f"Total pesan (non-kosong): {len(entry_ok) + len(followup_ok) + len(unrecognized)} / {total}")
    print(f"  Entry signal terparsing : {len(entry_ok)}")
    print(f"  Follow-up terparsing    : {len(followup_ok)}")
    print(f"  TIDAK dikenali          : {len(unrecognized)}")
    print()

    symbols = Counter(s.symbol for _, s in entry_ok)
    print("Simbol entry signal yang terdeteksi:")
    for sym, count in symbols.most_common():
        print(f"  {sym}: {count}")
    print()

    kinds_counter = Counter()
    for _, f in followup_ok:
        if not f.kinds:
            kinds_counter["info_only (tidak ada aksi otomatis)"] += 1
        for k in f.kinds:
            kinds_counter[k] += 1
    print("Follow-up kinds terdeteksi:")
    for kind, count in kinds_counter.most_common():
        print(f"  {kind}: {count}")
    print()

    with open(UNRECOGNIZED_PATH, "w") as f:
        for row in unrecognized:
            f.write(json.dumps(row, default=str) + "\n")
    print(f"{len(unrecognized)} pesan tidak dikenali disimpan ke {UNRECOGNIZED_PATH} untuk direview manual.")

    print("\nContoh 20 pesan tidak dikenali (dipotong 100 karakter pertama):")
    for row in unrecognized[:20]:
        preview = (row.get("text") or "").replace("\n", " | ")[:100]
        print(f"  #{row['message_id']}: {preview}")


if __name__ == "__main__":
    main()
