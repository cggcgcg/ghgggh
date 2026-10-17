import uuid
from datetime import datetime, timezone

from app.database import get_connection


def create_space_message(space_id, from_user, text="", msg_type="text", audio_data=None, waveform=None,
                         reply_to=None, forwarded_from=None):
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO space_messages
            (id, space_id, from_user, text, created_at, type, audio_data, waveform, reply_to, forwarded_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, space_id, from_user, text, created_at, msg_type, audio_data, waveform,
             reply_to, forwarded_from),
        )
        conn.commit()

    return {
        "id": message_id,
        "space_id": space_id,
        "from_user": from_user,
        "text": text,
        "created_at": created_at,
        "type": msg_type,
        "audio_data": audio_data,
        "waveform": waveform,
        "reply_to": reply_to,
        "forwarded_from": forwarded_from,
        "pinned": 0,
    }


def get_space_message(message_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM space_messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def get_space_messages(space_id, viewer=None):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT space_messages.*, users.display_name AS sender_name, users.username AS sender_username
            FROM space_messages
            JOIN users ON users.id = space_messages.from_user
            WHERE space_messages.space_id = ?
              AND space_messages.id NOT IN (SELECT message_id FROM message_hidden WHERE user_id = ?)
            ORDER BY space_messages.created_at ASC
            """,
            (space_id, viewer or ""),
        ).fetchall()
    return [dict(row) for row in rows]


def set_space_pinned(message_id, pinned):
    msg = get_space_message(message_id)
    if not msg:
        return None
    with get_connection() as conn:
        if pinned:
            conn.execute("UPDATE space_messages SET pinned = 0 WHERE space_id = ?", (msg["space_id"],))
        conn.execute("UPDATE space_messages SET pinned = ? WHERE id = ?", (1 if pinned else 0, message_id))
        conn.commit()
    return get_space_message(message_id)


def delete_space_message(message_id, user_id, for_everyone, is_manager):
    msg = get_space_message(message_id)
    if not msg:
        return False, "Message not found"
    with get_connection() as conn:
        if for_everyone:
            if msg["from_user"] != user_id and not is_manager:
                return False, "Нельзя удалить чужое сообщение у всех"
            conn.execute("DELETE FROM space_messages WHERE id = ?", (message_id,))
            conn.execute("DELETE FROM message_hidden WHERE message_id = ?", (message_id,))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO message_hidden (message_id, user_id) VALUES (?, ?)",
                (message_id, user_id),
            )
        conn.commit()
    return True, None