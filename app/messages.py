import uuid
from datetime import datetime, timezone

from app.database import get_connection


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_message(from_user, to_user, text="", msg_type="text", audio_data=None, waveform=None,
                   reply_to=None, forwarded_from=None):
    message_id = str(uuid.uuid4())
    created_at = _now()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO messages
            (id, from_user, to_user, text, created_at, type, audio_data, waveform, reply_to, forwarded_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, from_user, to_user, text, created_at, msg_type, audio_data, waveform,
             reply_to, forwarded_from),
        )
        conn.commit()

    return get_message(message_id)


def get_message(message_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def get_conversation(user_a, user_b, viewer=None):
    """Переписка двух людей. Сообщения, которые `viewer` удалил «только у
    себя», не возвращаются (по умолчанию viewer — user_a)."""
    viewer = viewer or user_a
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE ((from_user = ? AND to_user = ?)
                OR (from_user = ? AND to_user = ?))
              AND id NOT IN (SELECT message_id FROM message_hidden WHERE user_id = ?)
            ORDER BY created_at ASC
            """,
            (user_a, user_b, user_b, user_a, viewer),
        ).fetchall()

    return [dict(row) for row in rows]


def get_conversations(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT
                CASE WHEN from_user = ? THEN to_user ELSE from_user END AS other_id
            FROM messages
            WHERE from_user = ? OR to_user = ?
            """,
            (user_id, user_id, user_id),
        ).fetchall()

    return [row["other_id"] for row in rows]


def mark_read(reader_id, other_id):
    """Читатель открыл чат с other_id: всё, что тот ему прислал, считается
    увиденным (✓ -> ✓✓)."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE messages SET read_at = ? WHERE from_user = ? AND to_user = ? AND read_at IS NULL",
            (_now(), other_id, reader_id),
        )
        conn.commit()
        return cur.rowcount


def mark_played(message_id, user_id):
    """Получатель прослушал голосовое / посмотрел кружочек (✓✓ серые -> синие)."""
    with get_connection() as conn:
        now = _now()
        cur = conn.execute(
            """
            UPDATE messages
            SET played_at = COALESCE(played_at, ?), read_at = COALESCE(read_at, ?)
            WHERE id = ? AND to_user = ?
            """,
            (now, now, message_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_pinned(message_id, user_id, pinned):
    """Закрепить/открепить. Один закреплённый на чат: новое закрепление
    снимает предыдущее. Закреплять могут оба участника."""
    msg = get_message(message_id)
    if not msg or user_id not in (msg["from_user"], msg["to_user"]):
        return None
    with get_connection() as conn:
        if pinned:
            conn.execute(
                """
                UPDATE messages SET pinned = 0
                WHERE (from_user = ? AND to_user = ?) OR (from_user = ? AND to_user = ?)
                """,
                (msg["from_user"], msg["to_user"], msg["to_user"], msg["from_user"]),
            )
        conn.execute("UPDATE messages SET pinned = ? WHERE id = ?", (1 if pinned else 0, message_id))
        conn.commit()
    return get_message(message_id)


def delete_message(message_id, user_id, for_everyone):
    """Своё сообщение можно удалить у всех или только у себя; чужое — только
    у себя. Возвращает (ok, error)."""
    msg = get_message(message_id)
    if not msg or user_id not in (msg["from_user"], msg["to_user"]):
        return False, "Message not found"

    with get_connection() as conn:
        if for_everyone:
            if msg["from_user"] != user_id:
                return False, "Чужое сообщение можно удалить только у себя"
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.execute("DELETE FROM message_hidden WHERE message_id = ?", (message_id,))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO message_hidden (message_id, user_id) VALUES (?, ?)",
                (message_id, user_id),
            )
        conn.commit()
    return True, None