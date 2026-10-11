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


def get_contact_aliases(owner_id):
    """{contact_user_id: alias} для всех контактов этого владельца, где
    алиас задан (пустые/не заданные не включаются)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT contact_user_id, alias FROM contacts WHERE owner_id = ? AND alias IS NOT NULL AND alias != ''",
            (owner_id,),
        ).fetchall()
    return {row["contact_user_id"]: row["alias"] for row in rows}


def set_contact_alias(owner_id, contact_user_id, alias):
    """Личный никнейм контакта — виден только owner_id, не меняет
    настоящее имя/username человека. Пустая строка — сбросить алиас."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE contacts SET alias = ? WHERE owner_id = ? AND contact_user_id = ?",
            (alias or None, owner_id, contact_user_id),
        )
        conn.commit()


def remove_contact(owner_id, contact_user_id):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM contacts WHERE owner_id = ? AND contact_user_id = ?",
            (owner_id, contact_user_id),
        )
        conn.commit()

    return get_contact_ids(owner_id)