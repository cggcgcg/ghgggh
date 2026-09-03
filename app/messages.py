import uuid
from datetime import datetime, timezone

from app.database import get_connection


def create_message(from_user, to_user, text="", msg_type="text", audio_data=None):
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO messages
            (id, from_user, to_user, text, created_at, type, audio_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                from_user,
                to_user,
                text,
                created_at,
                msg_type,
                audio_data,
            ),
        )
        conn.commit()

    return {
        "id": message_id,
        "from_user": from_user,
        "to_user": to_user,
        "text": text,
        "created_at": created_at,
        "type": msg_type,
        "audio_data": audio_data,
    }


def get_conversation(user_a, user_b):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE (from_user = ? AND to_user = ?)
               OR (from_user = ? AND to_user = ?)
            ORDER BY created_at ASC
            """,
            (user_a, user_b, user_b, user_a),
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