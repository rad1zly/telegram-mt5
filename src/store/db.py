import os
import sqlite3
from contextlib import closing
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    date_utc TEXT NOT NULL,
    text TEXT,
    reply_to_msg_id INTEGER,
    raw_json TEXT,
    received_at TEXT NOT NULL,
    UNIQUE(channel, message_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    action TEXT,
    symbol TEXT,
    entry TEXT,
    sl REAL,
    tp TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    ticket INTEGER,
    symbol TEXT,
    lot REAL,
    open_price REAL,
    sl REAL,
    tp REAL,
    tp1_hit INTEGER DEFAULT 0,
    be_moved INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open',
    opened_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    signal_id INTEGER,
    kind TEXT,
    raw_text TEXT,
    processed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    text TEXT,
    edited_at_utc TEXT,
    received_at TEXT NOT NULL
);
"""


class Database:
    """Thin sync sqlite wrapper. Called via run_in_executor from async code
    once the orchestrator (Fase 3+) is in place; volume in Fase 1 is low
    enough to call directly from the Telethon event handler."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path

    def init_schema(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def insert_message(self, row: dict) -> bool:
        """Returns True if inserted, False if it was a duplicate (channel, message_id)."""
        with closing(sqlite3.connect(self.path)) as conn:
            try:
                conn.execute(
                    """INSERT INTO messages
                       (message_id, channel, date_utc, text, reply_to_msg_id, raw_json, received_at)
                       VALUES (:message_id, :channel, :date_utc, :text, :reply_to_msg_id, :raw_json, :received_at)""",
                    row,
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def count_messages(self) -> int:
        with closing(sqlite3.connect(self.path)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM messages")
            return cur.fetchone()[0]

    def insert_edit(self, row: dict) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                """INSERT INTO message_edits
                   (message_id, channel, text, edited_at_utc, received_at)
                   VALUES (:message_id, :channel, :text, :edited_at_utc, :received_at)""",
                row,
            )
            conn.commit()

    def insert_position(self, row: dict) -> int:
        with closing(sqlite3.connect(self.path)) as conn:
            cur = conn.execute(
                """INSERT INTO positions
                   (signal_id, ticket, symbol, lot, open_price, sl, tp, status, opened_at)
                   VALUES (:signal_id, :ticket, :symbol, :lot, :open_price, :sl, :tp, :status, :opened_at)""",
                row,
            )
            conn.commit()
            return cur.lastrowid

    def get_open_position_by_symbol(self, symbol: str) -> Optional[dict]:
        """Posisi terbuka TERBARU untuk simbol ini. Dipakai untuk mencocokkan
        follow-up ke posisi, karena channel post follow-up sebagai pesan
        biasa (bukan reply), tanpa reply_to_msg_id."""
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM positions WHERE symbol = ? AND status = 'open' "
                "ORDER BY opened_at DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def mark_be_moved(self, position_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE positions SET be_moved = 1 WHERE id = ?", (position_id,))
            conn.commit()

    def mark_tp1_hit(self, position_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE positions SET tp1_hit = 1 WHERE id = ?", (position_id,))
            conn.commit()

    def close_position(self, position_id: int, closed_at: str) -> None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE positions SET status = 'closed', closed_at = ? WHERE id = ?",
                (closed_at, position_id),
            )
            conn.commit()

    def count_positions_opened_since(self, since_iso: str) -> int:
        with closing(sqlite3.connect(self.path)) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE opened_at >= ?",
                (since_iso,),
            )
            return cur.fetchone()[0]
