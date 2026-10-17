"""Ссылки-приглашения в группу: можно ограничить по времени и по числу
использований, отозвать в любой момент. Работают и для приватных групп."""

import secrets
from datetime import datetime, timedelta, timezone

from app.database import get_connection
from app.workspace import add_member, get_space, is_member


def _now():
    return datetime.now(timezone.utc)


def create_invite(space_id, created_by, hours=None, max_uses=None):
    code = secrets.token_urlsafe(9)
    expires_at = (_now() + timedelta(hours=float(hours))).isoformat() if hours else None
    max_uses = int(max_uses) if max_uses else None
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO space_invites (code, space_id, created_by, created_at, expires_at, max_uses)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code, space_id, created_by, _now().isoformat(), expires_at, max_uses),
        )
        conn.commit()
    return get_invite(code)


def _status(row):
    if row["revoked"]:
        return "revoked"
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= _now():
        return "expired"
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        return "exhausted"
    return "active"


def get_invite(code):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM space_invites WHERE code = ?", (code,)).fetchone()
    if not row:
        return None
    invite = dict(row)
    invite["status"] = _status(row)
    return invite


def list_invites(space_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM space_invites WHERE space_id = ? AND revoked = 0 ORDER BY created_at DESC",
            (space_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["status"] = _status(row)
        result.append(item)
    return result


def revoke_invite(space_id, code):
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE space_invites SET revoked = 1 WHERE code = ? AND space_id = ?", (code, space_id)
        )
        conn.commit()
    return cur.rowcount > 0


def preview(code):
    """Что показать человеку перед вступлением: название, описание, число участников."""
    invite = get_invite(code)
    if not invite:
        return None
    space = get_space(invite["space_id"])
    if not space:
        return None
    from app.workspace import get_members
    return {
        "code": code,
        "status": invite["status"],
        "space_id": space["id"],
        "name": space["name"],
        "description": space.get("description") or "",
        "photo": space.get("photo"),
        "members": len(get_members(space["id"])),
    }


def use_invite(code, user_id):
    """Вступить по ссылке. Возвращает (space, error)."""
    invite = get_invite(code)
    if not invite:
        return None, "Ссылка не найдена"
    if invite["status"] != "active":
        reasons = {"revoked": "Ссылка отозвана", "expired": "Срок действия ссылки истёк",
                   "exhausted": "Ссылка уже использована максимальное число раз"}
        return None, reasons.get(invite["status"], "Ссылка недействительна")
    space = get_space(invite["space_id"])
    if not space:
        return None, "Группа не найдена"
    if is_member(space["id"], user_id):
        return space, None
    add_member(space["id"], user_id, role="member")
    with get_connection() as conn:
        conn.execute("UPDATE space_invites SET uses = uses + 1 WHERE code = ?", (code,))
        conn.commit()
    return get_space(space["id"]), None