import uuid
from datetime import datetime, timezone

from app.database import get_connection


def add_contact(owner_id, contact_user_id):
    contact_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO contacts (id, owner_id, contact_user_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (contact_id, owner_id, contact_user_id, created_at),
        )
        conn.commit()

    return get_contact_ids(owner_id)


def get_contact_ids(owner_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT contact_user_id FROM contacts WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()

    return [row["contact_user_id"] for row in rows]


def remove_contact(owner_id, contact_user_id):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM contacts WHERE owner_id = ? AND contact_user_id = ?",
            (owner_id, contact_user_id),
        )
        conn.commit()

    return get_contact_ids(owner_id)