import uuid
from datetime import datetime, timezone

from app.database import get_connection


def create_space_message(space_id, from_user, text="", msg_type="text", audio_data=None, waveform=None):
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO space_messages
            (id, space_id, from_user, text, created_at, type, audio_data, waveform)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, space_id, from_user, text, created_at, msg_type, audio_data, waveform),
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
    }


def get_space_messages(space_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT space_messages.*, users.display_name AS sender_name, users.username AS sender_username
            FROM space_messages
            JOIN users ON users.id = space_messages.from_user
            WHERE space_messages.space_id = ?
            ORDER BY space_messages.created_at ASC
            """,
            (space_id,),
        ).fetchall()
    return [dict(row) for row in rows]