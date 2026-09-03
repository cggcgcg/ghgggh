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


def create_space(owner_id, name, kind):
    space = {"id": str(uuid.uuid4()), "owner_id": owner_id, "name": name, "kind": kind, "created_at": datetime.now(timezone.utc).isoformat()}
    with get_connection() as conn:
        conn.execute("INSERT INTO spaces (id, owner_id, name, kind, created_at) VALUES (?, ?, ?, ?, ?)", tuple(space.values()))
        conn.commit()
    return space


def get_spaces(owner_id):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM spaces WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)).fetchall()
    return [dict(row) for row in rows]


def create_device(user_id, name):
    device = {"id": str(uuid.uuid4()), "user_id": user_id, "name": name, "created_at": datetime.now(timezone.utc).isoformat()}
    with get_connection() as conn:
        conn.execute("INSERT INTO devices (id, user_id, name, created_at) VALUES (?, ?, ?)", tuple(device.values()))
        conn.commit()
    return device


def get_devices(user_id):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM devices WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [dict(row) for row in rows]
