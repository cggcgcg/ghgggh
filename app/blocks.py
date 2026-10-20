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

# =====================================================
# Жалобы и модерация
# =====================================================
import json
import os
import uuid

REPORT_REASONS = {
    "spam": "Спам",
    "abuse": "Оскорбления, травля",
    "scam": "Мошенничество",
    "inappropriate": "Неприемлемый контент",
    "other": "Другое",
}
REVIEW_THRESHOLD = 3   # столько РАЗНЫХ людей — и аккаунт помечается «Требует проверки»


def is_admin(user_id):
    """Этот аккаунт ВООБЩЕ может быть админом. Админы задаются ТОЛЬКО переменными
    окружения на сервере (из приложения их изменить нельзя):
      ADMIN_IDS       — внутренние id аккаунтов через запятую (надёжно: id не занять);
      ADMIN_USERNAMES — запасной вариант по username (если имя свободно, его может
                        занять чужой человек — поэтому для входа всё равно нужен ключ).
    Без ADMIN_KEY админ-панель отключена полностью (см. check_admin_key)."""
    if not user_id or not os.environ.get("ADMIN_KEY", ""):
        return False
    ids = {n.strip() for n in os.environ.get("ADMIN_IDS", "").split(",") if n.strip()}
    if user_id in ids:
        return True
    names = {n.strip().lower() for n in os.environ.get("ADMIN_USERNAMES", "").split(",") if n.strip()}
    if not names:
        return False
    with get_connection() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["username"].lower() in names)


# Защита ключа от подбора: 5 неверных попыток подряд — блокировка на 15 минут.
import hmac
import threading
import time as _time

_key_fail = {"count": 0, "until": 0.0}
_key_lock = threading.Lock()


def check_admin_key(provided):
    """Возвращает (ok, error_text, http_status)."""
    secret = os.environ.get("ADMIN_KEY", "")
    if not secret:
        return False, "Админ-ключ не настроен на сервере (переменная ADMIN_KEY)", 503
    with _key_lock:
        if _key_fail["until"] > _time.time():
            return False, "Слишком много неверных попыток, подождите 15 минут", 429
    if provided and hmac.compare_digest(str(provided).encode(), secret.encode()):
        with _key_lock:
            _key_fail["count"] = 0
        return True, "", 200
    with _key_lock:
        _key_fail["count"] += 1
        if _key_fail["count"] >= 5:
            _key_fail["until"] = _time.time() + 15 * 60
            _key_fail["count"] = 0
    return False, "Неверный ключ", 403


def _label(row):
    t = row["type"] or "text"
    if t == "text":
        return (row["text"] or "")[:300]
    if t == "call":
        return "[звонок]"
    return {"voice": "[голосовое]", "video": "[кружок]", "image": "[фото]", "file": "[файл: %s]" % (row["text"] or "")}.get(t, "[вложение]")


def _evidence(conn, reporter_id, message_id, target_id):
    """Само сообщение + до 9 предыдущих в том же чате (только то, что видел автор жалобы)."""
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone() if message_id else None
    space_id = None
    rows = []
    if msg:
        if reporter_id not in (msg["from_user"], msg["to_user"]):
            return None, None, None
        rows = conn.execute(
            """SELECT m.*, u.display_name AS sender FROM messages m JOIN users u ON u.id = m.from_user
               WHERE ((m.from_user=? AND m.to_user=?) OR (m.from_user=? AND m.to_user=?))
                 AND m.created_at <= ? AND m.id NOT IN (SELECT message_id FROM message_hidden WHERE user_id = ?)
               ORDER BY m.created_at DESC LIMIT 10""",
            (msg["from_user"], msg["to_user"], msg["to_user"], msg["from_user"], msg["created_at"], reporter_id)).fetchall()
        target_id = msg["from_user"]
    else:
        smsg = conn.execute("SELECT * FROM space_messages WHERE id = ?", (message_id,)).fetchone() if message_id else None
        if smsg:
            if not conn.execute("SELECT 1 FROM space_members WHERE space_id=? AND user_id=?", (smsg["space_id"], reporter_id)).fetchone():
                return None, None, None
            space_id = smsg["space_id"]
            rows = conn.execute(
                """SELECT m.*, u.display_name AS sender FROM space_messages m JOIN users u ON u.id = m.from_user
                   WHERE m.space_id=? AND m.created_at <= ? AND m.id NOT IN (SELECT message_id FROM message_hidden WHERE user_id = ?)
                   ORDER BY m.created_at DESC LIMIT 10""", (space_id, smsg["created_at"], reporter_id)).fetchall()
            target_id = smsg["from_user"]
        elif target_id:
            rows = conn.execute(
                """SELECT m.*, u.display_name AS sender FROM messages m JOIN users u ON u.id = m.from_user
                   WHERE ((m.from_user=? AND m.to_user=?) OR (m.from_user=? AND m.to_user=?))
                     AND m.id NOT IN (SELECT message_id FROM message_hidden WHERE user_id = ?)
                   ORDER BY m.created_at DESC LIMIT 10""", (reporter_id, target_id, target_id, reporter_id, reporter_id)).fetchall()
    evidence = [{"from": r["sender"], "text": _label(r), "at": r["created_at"]} for r in reversed(rows)]
    return evidence, target_id, space_id


def create_report(reporter_id, target_user_id, reason, details="", message_id=None):
    if reason not in REPORT_REASONS:
        return None, "Выберите причину жалобы"
    with get_connection() as conn:
        evidence, target_id, space_id = _evidence(conn, reporter_id, message_id, target_user_id)
        if evidence is None:
            return None, "Сообщение не найдено"
        if not target_id or target_id == reporter_id:
            return None, "Нельзя пожаловаться на себя"
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (target_id,)).fetchone():
            return None, "Пользователь не найден"
        # повторная жалоба того же человека на того же — обновляет прежнюю, а не множит счётчик
        old = conn.execute("SELECT id FROM reports WHERE reporter_id=? AND target_user_id=? AND status='open'",
                           (reporter_id, target_id)).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        rid = old["id"] if old else str(uuid.uuid4())
        if old:
            conn.execute("UPDATE reports SET reason=?, details=?, evidence=?, message_id=?, space_id=?, created_at=? WHERE id=?",
                         (reason, details[:500], json.dumps(evidence, ensure_ascii=False), message_id, space_id, now, rid))
        else:
            conn.execute(
                "INSERT INTO reports (id, reporter_id, target_user_id, space_id, message_id, reason, details, evidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, reporter_id, target_id, space_id, message_id, reason, details[:500], json.dumps(evidence, ensure_ascii=False), now))
        conn.commit()
    return {"id": rid, "target_user_id": target_id}, None


def list_reports_for_admin():
    """Открытые жалобы, сгруппированные по обвиняемому. «Требует проверки» — вверху."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.*, ru.display_name AS reporter_name, ru.username AS reporter_username,
                      tu.display_name AS target_name, tu.username AS target_username, tu.banned AS target_banned,
                      sp.name AS space_name
               FROM reports r
               JOIN users ru ON ru.id = r.reporter_id
               JOIN users tu ON tu.id = r.target_user_id
               LEFT JOIN spaces sp ON sp.id = r.space_id
               WHERE r.status = 'open' ORDER BY r.created_at DESC""").fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault(r["target_user_id"], {
            "target": {"id": r["target_user_id"], "username": r["target_username"], "display_name": r["target_name"], "banned": bool(r["target_banned"])},
            "reports": []})
        g["reports"].append({
            "id": r["id"], "reporter": r["reporter_name"], "reporter_username": r["reporter_username"],
            "reason": REPORT_REASONS.get(r["reason"], r["reason"]), "details": r["details"],
            "evidence": json.loads(r["evidence"] or "[]"), "space": r["space_name"], "created_at": r["created_at"]})
    out = list(groups.values())
    for g in out:
        g["reporters"] = len({x["reporter_username"] for x in g["reports"]})
        g["needs_review"] = g["reporters"] >= REVIEW_THRESHOLD
    out.sort(key=lambda g: (not g["needs_review"], -g["reporters"], g["reports"][0]["created_at"]))
    return out


def resolve_reports(target_user_id, action):
    """action: dismiss — отклонить жалобы; ban — заблокировать аккаунт; unban — разблокировать."""
    from app.users import set_banned
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        if action == "unban":
            set_banned(target_user_id, False)
            return True
        status = "actioned" if action == "ban" else "dismissed"
        conn.execute("UPDATE reports SET status=?, resolved_at=? WHERE target_user_id=? AND status='open'",
                     (status, now, target_user_id))
        conn.commit()
    if action == "ban":
        set_banned(target_user_id, True)
    return action in ("ban", "dismiss")