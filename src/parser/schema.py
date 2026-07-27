from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    """Hasil parsing pesan entry signal. Dibangun di Fase 2 dari korpus
    tests/fixtures/signals.jsonl yang dikumpulkan Fase 1."""

    message_id: int
    action: str  # BUY | SELL | BUY_LIMIT | SELL_LIMIT | BUY_STOP | SELL_STOP
    symbol: str
    entry: Optional[float] = None
    entry_range: Optional[tuple[float, float]] = None
    sl: Optional[float] = None
    tp: list[float] = field(default_factory=list)


@dataclass
class FollowUp:
    """Pesan susulan yang terkait sebuah signal, misal 'SL to BE' atau 'TP1 hit'."""

    message_id: int
    reply_to_msg_id: Optional[int]
    kind: str  # "move_sl_be" | "partial_close_tp1" | "close_all" | "info"
    raw_text: str
