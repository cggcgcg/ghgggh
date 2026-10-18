import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.database import get_connection

DEFAULT_SETTINGS = {
    "language": "ru",
    "app_theme": "night",
    "chat_theme": "aurora",
    "font_size": "medium",
    "design": "glass",
    "density": "comfortable",
}


def get_settings(user_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row)


def update_settings(user_id, values):
    settings = get_settings(user_id)
    allowed = set(DEFAULT_SETTINGS)
    settings.update({key: str(value) for key, value in values.items() if key in allowed})
    with get_connection() as conn:
        conn.execute(
            """UPDATE user_settings SET language=?, app_theme=?, chat_theme=?, font_size=?, design=?, density=?
               WHERE user_id=?""",
            (settings["language"], settings["app_theme"], settings["chat_theme"], settings["font_size"], settings["design"], settings["density"], user_id),
        )
        conn.commit()
    return get_settings(user_id)


# =====================================================
# Каналы / группы (spaces)
# =====================================================

# Права ОБЫЧНЫХ участников группы. Владелец и админы могут всё.
#   send        — писать текстовые сообщения
#   media       — отправлять голосовые, кружочки, фото и файлы
#   add_members — добавлять людей по username
#   invite_link — создавать ссылки-приглашения
#   pin         — закреплять сообщения
#   calls       — начинать групповой звонок
DEFAULT_PERMISSIONS = {
    "send": True,
    "media": True,
    "add_members": False,
    "invite_link": False,
    "pin": False,
    "calls": True,
}


def _parse_permissions(raw):
    perms = dict(DEFAULT_PERMISSIONS)
    try:
        saved = json.loads(raw or "{}")
    except (TypeError, ValueError):
        saved = {}
    for key in DEFAULT_PERMISSIONS:
        if key in saved:
            perms[key] = bool(saved[key])
    return perms


def _row_to_space(row):
    space = dict(row)
    space["is_private"] = bool(space.get("is_private"))
    space["permissions"] = _parse_permissions(space.get("permissions"))
    return space


def create_space(owner_id, name, kind, description="", photo=None, is_private=False):
    space_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO spaces
            (id, owner_id, name, kind, created_at, photo, description, background, is_private, invite_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            """,
            (space_id, owner_id, name, kind, created_at, photo, description, 1 if is_private else 0),
        )
        conn.execute(
            "INSERT INTO space_members (space_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (space_id, owner_id, created_at),
        )
        conn.commit()

    return get_space(space_id)


def get_space(space_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
    return _row_to_space(row) if row else None


def search_public_spaces(query):
    # Только публичные — приватные намеренно не находятся через поиск/по id,
    # попасть в них можно только если владелец/админ добавит конкретного
    # человека вручную по его username (см. договорённость про приватность).
    # Матчим и по точному id, и по названию — username пространств пока нет.
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM spaces WHERE is_private = 0 AND (id = ? OR LOWER(name) LIKE LOWER(?)) ORDER BY created_at DESC LIMIT 20",
            (query, f"%{query}%"),
        ).fetchall()
    return [_row_to_space(row) for row in rows]


def get_spaces(user_id):
    """Пространства, где user_id состоит участником (владелец тоже
    является участником — см. create_space и бэкфилл при миграции)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT spaces.*, space_members.role AS my_role, space_members.muted AS my_muted
            FROM spaces
            JOIN space_members ON space_members.space_id = spaces.id
            WHERE space_members.user_id = ?
            ORDER BY spaces.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_space(row) for row in rows]


def update_space(space_id, values):
    space = get_space(space_id)
    if not space:
        return None
    allowed = {"name", "description", "photo", "background"}
    for key in allowed:
        if key in values and values[key] is not None:
            space[key] = values[key]
    with get_connection() as conn:
        conn.execute(
            "UPDATE spaces SET name=?, description=?, photo=?, background=? WHERE id=?",
            (space["name"], space["description"], space["photo"], space["background"], space_id),
        )
        conn.commit()
    return get_space(space_id)


def set_privacy(space_id, is_private):
    with get_connection() as conn:
        conn.execute("UPDATE spaces SET is_private=? WHERE id=?", (1 if is_private else 0, space_id))
        conn.commit()
    return get_space(space_id)


def delete_space(space_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM space_members WHERE space_id = ?", (space_id,))
        conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
        conn.commit()


# ---- участники / роли ----

def get_member_role(space_id, user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        ).fetchone()
    return row["role"] if row else None


def is_member(space_id, user_id):
    return get_member_role(space_id, user_id) is not None


def can_manage(space_id, user_id):
    # Права админа приравнены к правам создателя (owner) — договорённость:
    # владелец и админ одинаково меняют настройки, добавляют/удаляют
    # участников и назначают других админов.
    role = get_member_role(space_id, user_id)
    return role in ("owner", "admin")


def set_permissions(space_id, values):
    perms = _parse_permissions(json.dumps(get_space(space_id)["permissions"]))
    for key in DEFAULT_PERMISSIONS:
        if key in values:
            perms[key] = bool(values[key])
    with get_connection() as conn:
        conn.execute("UPDATE spaces SET permissions = ? WHERE id = ?", (json.dumps(perms), space_id))
        conn.commit()
    return perms


def is_muted(space_id, user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT muted FROM space_members WHERE space_id=? AND user_id=?", (space_id, user_id)
        ).fetchone()
    return bool(row and row["muted"])


def set_member_muted(space_id, user_id, muted):
    with get_connection() as conn:
        conn.execute(
            "UPDATE space_members SET muted=? WHERE space_id=? AND user_id=?",
            (1 if muted else 0, space_id, user_id),
        )
        conn.commit()
    return get_members(space_id)


def can_do(space_id, user_id, action):
    """Может ли user_id выполнить действие (send/media/add_members/invite_link/
    pin/calls). Владелец и админы — всегда; обычный участник — по правам
    группы. Заглушенный участник не может ни писать, ни слать медиа."""
    role = get_member_role(space_id, user_id)
    if role is None:
        return False
    if role in ("owner", "admin"):
        return True
    if action in ("send", "media") and is_muted(space_id, user_id):
        return False
    space = get_space(space_id)
    return bool(space and space["permissions"].get(action))


def add_member(space_id, user_id, role="member"):
    joined_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO space_members (space_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
            (space_id, user_id, role, joined_at),
        )
        conn.commit()
    return get_members(space_id)


def remove_member(space_id, user_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM space_members WHERE space_id=? AND user_id=?", (space_id, user_id))
        conn.commit()
    return get_members(space_id)


def set_member_role(space_id, user_id, role):
    if role not in ("owner", "admin", "member"):
        return None
    with get_connection() as conn:
        conn.execute(
            "UPDATE space_members SET role=? WHERE space_id=? AND user_id=?",
            (role, space_id, user_id),
        )
        conn.commit()
    return get_members(space_id)


def get_members(space_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT space_members.user_id, space_members.role, space_members.joined_at,
                   space_members.muted, users.username, users.display_name
            FROM space_members
            JOIN users ON users.id = space_members.user_id
            WHERE space_members.space_id = ?
            ORDER BY
                CASE space_members.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                space_members.joined_at ASC
            """,
            (space_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def join_space(space_id, user_id):
    """Вступление в ПУБЛИЧНОЕ пространство, зная его id (найдено как обычный
    собеседник через поиск/ID). Приватные так вступить не дадут — туда может
    добавить только владелец/админ вручную по username (см. add_member)."""
    space = get_space(space_id)
    if not space:
        return None, "Space not found"

    if is_member(space_id, user_id):
        return space, None

    if space["is_private"]:
        return None, "This space is private — ask an admin to add you"

    add_member(space_id, user_id, role="member")
    return get_space(space_id), None


def create_device(user_id, name):
    device = {"id": str(uuid.uuid4()), "user_id": user_id, "name": name, "created_at": datetime.now(timezone.utc).isoformat()}
    with get_connection() as conn:
        conn.execute("INSERT INTO devices (id, user_id, name, created_at) VALUES (?, ?, ?, ?)", tuple(device.values()))
        conn.commit()
    return device


def get_devices(user_id):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM devices WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [dict(row) for row in rows]


# ---------- ссылки-приглашения ----------


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