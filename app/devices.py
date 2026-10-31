"""Сеансы устройств: каждый вход в аккаунт создаёт «устройство» с секретным токеном.

В базе хранится только хэш токена. Сам токен лежит в браузере/приложении
и отправляется в заголовках X-Device-Id / X-Device-Token.
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone

from app.database import get_connection

MAX_DEVICES_PER_USER = 20
TOUCH_INTERVAL_SECONDS = 60  # как часто обновлять «последняя активность»


def _now():
    return datetime.now(timezone.utc)


def _hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def device_name_from_ua(user_agent):
    """Короткое имя вроде «Chrome · Windows» из User-Agent."""
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "YaBrowser" in ua:
        browser = "Yandex"
    elif "SamsungBrowser" in ua:
        browser = "Samsung Internet"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    # Порядок важен: у iPhone в UA есть «Mac OS X», у Android — «Linux».
    if "iPhone" in ua:
        system = "iPhone"
    elif "iPad" in ua:
        system = "iPad"
    elif "Android" in ua:
        system = "Android"
    elif "Windows" in ua:
        system = "Windows"
    elif "CrOS" in ua:
        system = "ChromeOS"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        system = "macOS"
    elif "Linux" in ua:
        system = "Linux"
    else:
        system = ""
    return f"{browser} · {system}" if system else browser


def register_device(user_id, user_agent=""):
    """Создаёт сеанс для только что вошедшего пользователя. Возвращает id и токен."""
    device_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    now = _now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO devices (id, user_id, name, created_at, token_hash, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, user_id, device_name_from_ua(user_agent), now, _hash(token), now),
        )
        # Держим не больше MAX_DEVICES_PER_USER сеансов: самые старые по активности удаляем.
        rows = conn.execute(
            "SELECT id FROM devices WHERE user_id = ? AND token_hash IS NOT NULL "
            "ORDER BY COALESCE(last_seen_at, created_at) DESC",
            (user_id,),
        ).fetchall()
        for row in rows[MAX_DEVICES_PER_USER:]:
            conn.execute("DELETE FROM devices WHERE id = ?", (row["id"],))
        conn.commit()
    return {"device_id": device_id, "device_token": token}


def authenticate(device_id, token):
    """Возвращает user_id, если сеанс живой и токен верный, иначе None."""
    device_id = str(device_id or "").strip()
    token = str(token or "").strip()
    if not device_id or not token:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, token_hash, last_seen_at FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if not row or not row["token_hash"]:
            return None
        if not hmac.compare_digest(row["token_hash"], _hash(token)):
            return None
        stale = True
        if row["last_seen_at"]:
            try:
                last = datetime.fromisoformat(row["last_seen_at"])
                stale = (_now() - last).total_seconds() >= TOUCH_INTERVAL_SECONDS
            except ValueError:
                stale = True
        if stale:
            conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (_now().isoformat(), device_id))
            conn.commit()
        return row["user_id"]


def list_devices(user_id, current_device_id=None):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_seen_at FROM devices "
            "WHERE user_id = ? AND token_hash IS NOT NULL "
            "ORDER BY COALESCE(last_seen_at, created_at) DESC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"] or row["created_at"],
            "current": row["id"] == current_device_id,
        }
        for row in rows
    ]


def revoke_device(user_id, device_id):
    """Завершает один сеанс. Возвращает True, если он существовал."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def revoke_other_devices(user_id, keep_device_id):
    """Завершает все сеансы, кроме текущего. Возвращает число завершённых."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM devices WHERE user_id = ? AND id != ? AND token_hash IS NOT NULL",
            (user_id, keep_device_id),
        )
        conn.commit()
        return cur.rowcount