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

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    contact_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, contact_user_id)
);

CREATE TABLE IF NOT EXISTS space_members (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS blocks (
    blocker_id TEXT NOT NULL,
    blocked_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS space_messages (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    from_user TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'text',
    audio_data TEXT,
    waveform TEXT
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

        if not _column_exists(conn, "messages", "waveform"):
            conn.execute(
                "ALTER TABLE messages ADD COLUMN waveform TEXT"
            )

        # Миграция: расширяем spaces (группы/каналы) — фото, описание, фон,
        # приватность и код-приглашение. Старые записи получают безопасные
        # дефолты (публичные, без фото).
        if not _column_exists(conn, "spaces", "photo"):
            conn.execute("ALTER TABLE spaces ADD COLUMN photo TEXT")

        if not _column_exists(conn, "spaces", "description"):
            conn.execute("ALTER TABLE spaces ADD COLUMN description TEXT NOT NULL DEFAULT ''")

        if not _column_exists(conn, "spaces", "background"):
            conn.execute("ALTER TABLE spaces ADD COLUMN background TEXT")

        if not _column_exists(conn, "spaces", "is_private"):
            conn.execute("ALTER TABLE spaces ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0")

        if not _column_exists(conn, "spaces", "invite_code"):
            conn.execute("ALTER TABLE spaces ADD COLUMN invite_code TEXT")

        conn.commit()

        # Бэкфилл: у каналов/групп, созданных до миграции, владелец мог не
        # попасть в space_members (эта таблица появилась позже) — без этого
        # они бы не видели свои же старые пространства в списке через
        # get_spaces(), который теперь читает именно из space_members.
        conn.execute(
            """
            INSERT OR IGNORE INTO space_members (space_id, user_id, role, joined_at)
            SELECT id, owner_id, 'owner', created_at FROM spaces
            """
        )
        conn.commit()