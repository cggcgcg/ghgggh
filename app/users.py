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