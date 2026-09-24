import secrets
import uuid
from datetime import datetime, timezone

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

def _new_invite_code():
    # Короткий urlsafe-код — не подбирается перебором и удобно вставляется
    # в ссылку-приглашение вида tgclone.example/join/<code>.
    return secrets.token_urlsafe(8)[:10]


def _row_to_space(row):
    space = dict(row)
    space["is_private"] = bool(space.get("is_private"))
    return space


def create_space(owner_id, name, kind, description="", photo=None, is_private=False):
    space_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    invite_code = _new_invite_code()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO spaces
            (id, owner_id, name, kind, created_at, photo, description, background, is_private, invite_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (space_id, owner_id, name, kind, created_at, photo, description, 1 if is_private else 0, invite_code),
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


def find_space_by_invite_code(invite_code):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM spaces WHERE invite_code = ?", (invite_code,)).fetchone()
    return _row_to_space(row) if row else None


def search_public_spaces(query):
    # Только публичные — приватные намеренно не находятся через поиск,
    # попасть в них можно лишь по ссылке-приглашению или если владелец/админ
    # добавит конкретного человека вручную (см. договорённость про приватность).
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM spaces WHERE is_private = 0 AND LOWER(name) LIKE LOWER(?) ORDER BY created_at DESC LIMIT 20",
            (f"%{query}%",),
        ).fetchall()
    return [_row_to_space(row) for row in rows]


def get_spaces(user_id):
    """Пространства, где user_id состоит участником (владелец тоже
    является участником — см. create_space и бэкфилл при миграции)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT spaces.*, space_members.role AS my_role
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
                   users.username, users.display_name
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
    """Вступление в ПУБЛИЧНОЕ пространство, зная его id (найдено как
    обычный собеседник через поиск/ID) — приватные так вступить не дадут,
    для них есть только join_by_invite_code."""
    space = get_space(space_id)
    if not space:
        return None, "Space not found"

    if is_member(space_id, user_id):
        return space, None

    if space["is_private"]:
        return None, "This space is private — join by invite link only"

    add_member(space_id, user_id, role="member")
    return get_space(space_id), None


def join_by_invite_code(invite_code, user_id):
    space = find_space_by_invite_code(invite_code)
    if not space:
        return None, "Invalid invite link"
    if not is_member(space["id"], user_id):
        add_member(space["id"], user_id, role="member")
    return get_space(space["id"]), None


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