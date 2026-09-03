import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "tgclone.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_user TEXT NOT NULL,
    to_user TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        yield conn
    finally:
        conn.close()


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)

        # Миграция: добавляем поддержку голосовых сообщений
        # в уже существующую таблицу messages (не ломает старые данные).
        if not _column_exists(conn, "messages", "type"):
            conn.execute(
                "ALTER TABLE messages ADD COLUMN type TEXT NOT NULL DEFAULT 'text'"
            )

        if not _column_exists(conn, "messages", "audio_data"):
            conn.execute(
                "ALTER TABLE messages ADD COLUMN audio_data TEXT"
            )

        conn.commit()