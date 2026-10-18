import uuid
from datetime import datetime, timezone

from app.database import get_connection


def create_user(username, display_name):
    user_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users
            (id, username, display_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                display_name,
                created_at
            ),
        )
        conn.commit()

    return {
        "id": user_id,
        "username": username,
        "display_name": display_name,
        "created_at": created_at,
    }


def get_user(user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if row:
        return dict(row)

    return None


def find_by_username(username):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM users
            WHERE LOWER(username) = LOWER(?)
            """,
            (username,),
        ).fetchone()

    if row:
        return dict(row)

    return None


def update_user(user_id, username, display_name):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET username = ?, display_name = ? WHERE id = ?",
            (username, display_name, user_id),
        )
        conn.commit()
    return get_user(user_id)

# ---------- статус «в сети» ----------
ONLINE_WINDOW_SECONDS = 45
VISIBILITY_LEVELS = ("everyone", "contacts", "nobody")


def touch_user(user_id):
    with get_connection() as conn:
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), user_id))
        conn.commit()


def set_status_visibility(user_id, level):
    """everyone — видят все; contacts — только те, кто в моих контактах;
    nobody — никто (остальные видят «был(а) недавно»)."""
    if level not in VISIBILITY_LEVELS:
        return False
    with get_connection() as conn:
        conn.execute("UPDATE users SET status_visibility = ? WHERE id = ?", (level, user_id))
        conn.commit()
    return True


def get_presence(target_id, viewer_id=None):
    """Что показать зрителю: online / offline (+ last_seen) / recently (скрыто)."""
    from app.contacts import get_contact_ids
    with get_connection() as conn:
        row = conn.execute("SELECT last_seen, status_visibility FROM users WHERE id = ?", (target_id,)).fetchone()
    if not row:
        return None
    level = row["status_visibility"] or "everyone"
    result = {}
    if viewer_id == target_id:
        result["visibility"] = level          # свой статус человек видит всегда
    elif level == "nobody" or (level == "contacts" and viewer_id not in get_contact_ids(target_id)):
        result["status"] = "recently"
        return result
    last = row["last_seen"]
    if not last:
        result["status"] = "recently"
        return result
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    result["status"] = "online" if age <= ONLINE_WINDOW_SECONDS else "offline"
    result["last_seen"] = last
    return result


# ---------- «печатает…» / «записывает голосовое…» (только в памяти) ----------
import threading
import time as _time

ACTIVITY_TTL = 5.0
_activity_lock = threading.Lock()
_activity = {}   # (chat_key, user_id) -> (kind, expires_at)


def set_activity(user_id, chat_key, kind):
    """chat_key: для личного чата — id собеседника-получателя, для группы — id группы.
    kind: typing | voice | video | none."""
    with _activity_lock:
        if kind in ("typing", "voice", "video"):
            _activity[(chat_key, user_id)] = (kind, _time.time() + ACTIVITY_TTL)
        else:
            _activity.pop((chat_key, user_id), None)


def get_activity(chat_key, exclude_user=None):
    """Кто сейчас что делает в этом чате: [{user_id, kind}]."""
    now = _time.time()
    out = []
    with _activity_lock:
        for key in [k for k, v in _activity.items() if v[1] < now]:
            _activity.pop(key, None)
        for (ck, uid), (kind, _) in _activity.items():
            if ck == chat_key and uid != exclude_user:
                out.append({"user_id": uid, "kind": kind})
    return out


# ---------- блокировка аккаунта модератором, правила, удаление ----------
def is_banned(user_id):
    with get_connection() as conn:
        row = conn.execute("SELECT banned FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def set_banned(user_id, banned):
    with get_connection() as conn:
        conn.execute("UPDATE users SET banned = ? WHERE id = ?", (1 if banned else 0, user_id))
        conn.commit()


def accept_terms(user_id):
    with get_connection() as conn:
        conn.execute("UPDATE users SET terms_accepted_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), user_id))
        conn.commit()


def delete_account(user_id):
    """Полное удаление аккаунта и всего, что с ним связано."""
    with get_connection() as conn:
        owned = [r["id"] for r in conn.execute("SELECT id FROM spaces WHERE owner_id = ?", (user_id,)).fetchall()]
        for sid in owned:
            conn.execute("DELETE FROM space_messages WHERE space_id = ?", (sid,))
            conn.execute("DELETE FROM space_members WHERE space_id = ?", (sid,))
            conn.execute("DELETE FROM space_invites WHERE space_id = ?", (sid,))
            conn.execute("DELETE FROM space_reads WHERE space_id = ?", (sid,))
            conn.execute("DELETE FROM spaces WHERE id = ?", (sid,))
        for sql in (
            "DELETE FROM message_reactions WHERE user_id = ? OR message_id IN (SELECT id FROM messages WHERE from_user = ? OR to_user = ?)",
            "DELETE FROM message_hidden WHERE user_id = ?",
            "DELETE FROM messages WHERE from_user = ? OR to_user = ?",
            "DELETE FROM space_messages WHERE from_user = ?",
            "DELETE FROM space_members WHERE user_id = ?",
            "DELETE FROM space_reads WHERE user_id = ?",
            "DELETE FROM contacts WHERE owner_id = ? OR contact_user_id = ?",
            "DELETE FROM blocks WHERE blocker_id = ? OR blocked_id = ?",
            "DELETE FROM devices WHERE user_id = ?",
            "DELETE FROM user_settings WHERE user_id = ?",
            "DELETE FROM reports WHERE reporter_id = ? OR target_user_id = ?",
            "DELETE FROM users WHERE id = ?",
        ):
            n = sql.count("?")
            conn.execute(sql, (user_id,) * n)
        conn.commit()