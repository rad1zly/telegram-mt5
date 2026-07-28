"""Parser regex untuk pesan entry signal, dibangun dari korpus nyata di
tests/fixtures/signals.jsonl. Format channel yang diamati:

    <SYMBOL>

    <Buy/Sell> [now] [below/above] <entry atau rentang entry>

    <Target|tp>[.]: <tp1>, <tp2>, ...
    <Sl>[.]: <angka>

    [risk ...] [timeframe ...] [current price ...]  <- diabaikan

Prinsip sama dengan modul lain: kalau simbol, arah, SL, atau TP tidak
ketemu, return None — jangan mengarang nilai yang tidak ada di teks.
Pesan "Live Update" / follow-up sengaja ditolak di sini (lihat
followup.py), supaya tidak salah dianggap entry baru.
"""

import re
from typing import Optional

from src.parser.schema import Signal

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{2,9}$", re.IGNORECASE)

DIRECTION_RE = re.compile(
    r"\b(buy|sell)\b\s*(?:now\s+)?(?:below|above)?\s*"
    r"([\d,]+\.?\d*)\s*(?:-\s*([\d,]+\.?\d*))?",
    re.IGNORECASE,
)

TP_LINE_RE = re.compile(r"^\s*(?:target|tp)\.?\s*:?\s*(.+)$", re.IGNORECASE | re.MULTILINE)
SL_LINE_RE = re.compile(r"^\s*sl\.?\s*:?\s*([\d,]+\.?\d*)", re.IGNORECASE | re.MULTILINE)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _parse_number_list(raw: str) -> list[float]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(_to_float(part))
        except ValueError:
            continue
    return values


def parse_entry_signal(text: str, message_id: int) -> Optional[Signal]:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None

    first_line = lines[0]
    # "Live Update" / follow-up messages punya '|' di header atau kata "update" —
    # bukan entry baru, tolak di sini (ditangani followup.py).
    if "|" in first_line or "update" in first_line.lower():
        return None

    if not SYMBOL_RE.match(first_line):
        return None
    symbol = first_line.upper()

    direction_match = DIRECTION_RE.search(text)
    if not direction_match:
        return None
    action = direction_match.group(1).upper()
    entry_low = _to_float(direction_match.group(2))
    entry_high = _to_float(direction_match.group(3)) if direction_match.group(3) else None

    sl_match = SL_LINE_RE.search(text)
    if not sl_match:
        return None
    sl = _to_float(sl_match.group(1))

    tp_match = TP_LINE_RE.search(text)
    if not tp_match:
        return None
    tp = _parse_number_list(tp_match.group(1))
    if not tp:
        return None

    if entry_high is not None:
        return Signal(
            message_id=message_id,
            action=action,
            symbol=symbol,
            entry=None,
            entry_range=(entry_low, entry_high),
            sl=sl,
            tp=tp,
        )

    return Signal(
        message_id=message_id,
        action=action,
        symbol=symbol,
        entry=entry_low,
        entry_range=None,
        sl=sl,
        tp=tp,
    )
