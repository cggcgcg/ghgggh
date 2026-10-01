from datetime import datetime, timezone

from app.database import get_connection


def block_user(blocker_id, blocked_id):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
            (blocker_id, blocked_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def unblock_user(blocker_id, blocked_id):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
            (blocker_id, blocked_id),
        )
        conn.commit()


def is_blocked(blocker_id, blocked_id):
    """True, если blocker_id заблокировал blocked_id (т.е. blocked_id не
    может писать/звонить blocker_id)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
            (blocker_id, blocked_id),
        ).fetchone()
    return row is not None


def get_blocked_ids(blocker_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT blocked_id FROM blocks WHERE blocker_id = ? ORDER BY created_at DESC",
            (blocker_id,),
        ).fetchall()
    return [row["blocked_id"] for row in rows]