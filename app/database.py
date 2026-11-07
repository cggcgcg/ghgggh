import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

# Если задана переменная DATABASE_URL (Railway PostgreSQL) — работаем с PostgreSQL.
# Если её нет — по-старому на локальном SQLite-файле (удобно для разработки).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

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

-- Режим «сервер как труба» (RELAY_MODE): подтверждения получения от устройств
-- и временные события (прочитано, реакции, закрепление, удаление).
CREATE TABLE IF NOT EXISTS relay_acks (
    item_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    acked_at TEXT NOT NULL,
    PRIMARY KEY (item_id, device_id)
);

CREATE TABLE IF NOT EXISTS relay_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    chat TEXT NOT NULL,
    payload TEXT NOT NULL,
    targets TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Слой совместимости: остальной код пишет SQL в стиле SQLite (плейсхолдеры «?»,
# INSERT OR IGNORE / OR REPLACE, row["колонка"]). Для PostgreSQL переводим это
# на лету, чтобы модули users/messages/workspace/... менять не пришлось.
# ---------------------------------------------------------------------------

# Первичные ключи таблиц, где используется INSERT OR REPLACE.
_REPLACE_KEYS = {
    "message_reactions": ("message_id", "user_id"),
    "space_reads": ("space_id", "user_id"),
}
_OR_REPLACE_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I)
_OR_REPLACE_HEAD_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO", re.I)
_OR_IGNORE_HEAD_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.I)


@lru_cache(maxsize=1024)
def _to_pg(sql):
    s = sql.strip().rstrip(";")
    m = _OR_REPLACE_RE.match(s)
    if m:
        table = m.group(1).lower()
        cols = [c.strip() for c in m.group(2).split(",")]
        keys = _REPLACE_KEYS.get(table)
        if not keys:
            raise ValueError("INSERT OR REPLACE: не задан первичный ключ для таблицы " + table)
        s = _OR_REPLACE_HEAD_RE.sub("INSERT INTO", s, count=1)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in keys)
        if updates:
            s += f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
        else:
            s += " ON CONFLICT DO NOTHING"
    elif _OR_IGNORE_HEAD_RE.match(s):
        s = _OR_IGNORE_HEAD_RE.sub("INSERT INTO", s, count=1) + " ON CONFLICT DO NOTHING"
    # Сначала экранируем «%» (psycopg использует его как маркер), потом «?» -> «%s».
    return s.replace("%", "%%").replace("?", "%s")


class _Row(dict):
    """Строка результата: row["имя"], row[0], dict(row) — как sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []

    def make(values):
        return _Row(zip(names, values))

    return make


class _PgConnection:
    """Обёртка над psycopg-соединением с интерфейсом, похожим на sqlite3."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        return self._raw.execute(_to_pg(sql), tuple(params))

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=int(os.environ.get("DB_POOL_MIN", "2")),
                    max_size=int(os.environ.get("DB_POOL_MAX", "20")),
                    timeout=20,
                    kwargs={"row_factory": _row_factory},
                    check=ConnectionPool.check_connection,
                    open=False,
                )
                pool.open(wait=True, timeout=30)
                _pool = pool
    return _pool


@contextmanager
def get_connection():
    if USE_PG:
        pool = _get_pool()
        raw = pool.getconn()
        try:
            yield _PgConnection(raw)
        finally:
            # Как и в SQLite: всё, что не закоммичено, откатывается.
            try:
                raw.rollback()
            except Exception:  # noqa: BLE001
                pass
            pool.putconn(raw)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        try:
            yield conn
        finally:
            conn.close()


def _column_exists(conn, table, column):
    if USE_PG:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return row is not None
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


# Индексы под реальные запросы (чаты, непрочитанное, списки). Безопасны для обеих баз.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_user, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_user, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(to_user, read_at)",
    "CREATE INDEX IF NOT EXISTS idx_space_messages_space ON space_messages(space_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_space_members_user ON space_members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reactions_message ON message_reactions(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_relay_acks_device ON relay_acks(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_relay_events_created ON relay_events(created_at)",
)

_INIT_LOCK_ID = 727301  # чтобы две реплики не создавали таблицы одновременно


def init_db():
    with get_connection() as conn:
        if USE_PG:
            conn.execute("SELECT pg_advisory_lock(?)", (_INIT_LOCK_ID,))
            try:
                _init_schema(conn)
            finally:
                conn.rollback()
                conn.execute("SELECT pg_advisory_unlock(?)", (_INIT_LOCK_ID,))
                conn.commit()
        else:
            _init_schema(conn)


def _init_schema(conn):
    if USE_PG:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.commit()
    else:
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

    # Сеансы устройств: хэш токена входа и время последней активности.
    if not _column_exists(conn, "devices", "token_hash"):
        conn.execute("ALTER TABLE devices ADD COLUMN token_hash TEXT")
    if not _column_exists(conn, "devices", "last_seen_at"):
        conn.execute("ALTER TABLE devices ADD COLUMN last_seen_at TEXT")

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

    # Вход по коду из письма.
    big = "BIGINT" if USE_PG else "INTEGER"
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_codes ("
        f"email TEXT PRIMARY KEY, code_hash TEXT, expires_at {big} NOT NULL DEFAULT 0, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        f"last_sent {big} NOT NULL DEFAULT 0, "
        f"window_start {big} NOT NULL DEFAULT 0, sends INTEGER NOT NULL DEFAULT 0, "
        f"ticket_hash TEXT, ticket_expires {big} NOT NULL DEFAULT 0)"
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

    for statement in INDEXES:
        conn.execute(statement)
    conn.commit()