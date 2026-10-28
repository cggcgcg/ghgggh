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

CREATE TABLE IF NOT EXISTS space_invites (
    code TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    max_uses INTEGER,
    uses INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS space_reads (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    last_read_at TEXT NOT NULL,
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS pinned_chats (
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    pinned_at TEXT NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    space_id TEXT,
    message_id TEXT,
    reason TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS message_hidden (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
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

        # Миграция: статусы доставки (✓ / ✓✓), ответы, пересылка, закрепление.
        #   read_at   — собеседник открыл чат и увидел сообщение
        #   played_at — собеседник прослушал голосовое / посмотрел кружочек
        for table in ("messages", "space_messages"):
            for column, ddl in (
                ("reply_to", "TEXT"),
                ("forwarded_from", "TEXT"),
                ("pinned", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if not _column_exists(conn, table, column):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

        if not _column_exists(conn, "contacts", "alias"):
            conn.execute("ALTER TABLE contacts ADD COLUMN alias TEXT")

        for column in ("read_at", "played_at"):
            if not _column_exists(conn, "messages", column):
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")

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

        # Статус «в сети»: время последней активности и флаг «скрыть от всех».
        if not _column_exists(conn, "users", "last_seen"):
            conn.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
        if not _column_exists(conn, "users", "hide_status"):
            conn.execute("ALTER TABLE users ADD COLUMN hide_status INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "status_visibility"):
            conn.execute("ALTER TABLE users ADD COLUMN status_visibility TEXT NOT NULL DEFAULT 'everyone'")
            conn.execute("UPDATE users SET status_visibility = 'nobody' WHERE hide_status = 1")

        if not _column_exists(conn, "users", "banned"):
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "terms_accepted_at"):
            conn.execute("ALTER TABLE users ADD COLUMN terms_accepted_at TEXT")

        # Вход через Google: стабильный id аккаунта Google и почта.
        if not _column_exists(conn, "users", "google_sub"):
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        if not _column_exists(conn, "users", "email"):
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
            "ON users(google_sub) WHERE google_sub IS NOT NULL"
        )

        # Права участников группы (JSON) и «заглушенные» участники.
        if not _column_exists(conn, "spaces", "permissions"):
            conn.execute("ALTER TABLE spaces ADD COLUMN permissions TEXT NOT NULL DEFAULT '{}'")
        if not _column_exists(conn, "space_members", "muted"):
            conn.execute("ALTER TABLE space_members ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")

        # Каналы убраны из продукта: уже созданные становятся обычными группами.
        conn.execute("UPDATE spaces SET kind = 'group' WHERE kind = 'channel'")

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