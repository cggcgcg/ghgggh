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