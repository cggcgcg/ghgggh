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

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'ru',
    app_theme TEXT NOT NULL DEFAULT 'night',
    chat_theme TEXT NOT NULL DEFAULT 'aurora',
    font_size TEXT NOT NULL DEFAULT 'medium',
    design TEXT NOT NULL DEFAULT 'glass',
    density TEXT NOT NULL DEFAULT 'comfortable'
);

CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('group', 'channel')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
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