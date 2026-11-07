"""Режим «сервер как труба» (RELAY_MODE=on).

Сообщение лежит на сервере только до тех пор, пока ВСЕ устройства участников
не подтвердят, что сохранили его надёжно (локально и в Google Drive).
Потом оно удаляется. Если кто-то не в сети дольше срока хранения (по умолчанию
4 дня) — сообщение удаляется, а отправителю приходит событие «не доставлено».

Изменения сообщений (прочитано, реакция, закрепление, удаление) в этом режиме
идут не правкой строки в базе, а короткими временными «событиями»: устройства
забирают их, применяют у себя и подтверждают — после чего события удаляются.

Подтверждения (ack) бывают двух уровней:
  1 — устройство получило и сохранило у себя («доставлено»);
  2 — устройство надёжно сохранило (резервная копия в Drive) — можно удалять.
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from app.database import get_connection

MAX_PULL_MESSAGES = 100
MAX_PULL_EVENTS = 300
MAX_PULL_BYTES = 6 * 1024 * 1024
MAX_ACK_IDS = 500
EVENT_KINDS = ("read", "played", "react", "pin", "delete_all", "delete_me", "space_read")


def enabled():
    return os.environ.get("RELAY_MODE", "").strip().lower() in ("on", "1", "true", "yes")


def ttl():
    try:
        return timedelta(days=float(os.environ.get("RELAY_TTL_DAYS", "4")))
    except ValueError:
        return timedelta(days=4)


def _now_dt():
    return datetime.now(timezone.utc)


def _now():
    return _now_dt().isoformat()


def _parse(iso):
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _placeholders(n):
    return ",".join("?" for _ in range(n))


# ---------------------------------------------------------------- состояние
def started_at(conn=None):
    """Когда режим «труба» включили впервые. Сообщения старше этой отметки —
    «старая история»: её устройства забирают целиком, уведомлений о недоставке для неё нет."""
    own = conn is None
    if own:
        ctx = get_connection()
        conn = ctx.__enter__()
    try:
        row = conn.execute("SELECT value FROM relay_state WHERE key = 'started_at'").fetchone()
        if row:
            return row["value"]
        now = _now()
        conn.execute("INSERT OR IGNORE INTO relay_state (key, value) VALUES ('started_at', ?)", (now,))
        conn.commit()
        row = conn.execute("SELECT value FROM relay_state WHERE key = 'started_at'").fetchone()
        return row["value"] if row else now
    finally:
        if own:
            ctx.__exit__(None, None, None)


def init():
    if enabled():
        started_at()


# ---------------------------------------------------------------- устройства и участники
def _devices_of(conn, user_ids):
    ids = list(user_ids)
    if not ids:
        return []
    return conn.execute(
        f"SELECT id, user_id, created_at FROM devices WHERE token_hash IS NOT NULL AND user_id IN ({_placeholders(len(ids))})",
        ids,
    ).fetchall()


def _members(conn, space_id):
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM space_members WHERE space_id = ?", (space_id,)).fetchall()]


def _participants(conn, kind, row):
    if kind == "dm":
        return {row["from_user"], row["to_user"]}
    return set(_members(conn, row["space_id"]))


def _is_legacy(row, start):
    return start is not None and (row["created_at"] or "") < start


def _target_devices(conn, kind, row, start):
    users = _participants(conn, kind, row)
    devices = _devices_of(conn, users)
    if _is_legacy(row, start):
        return devices  # старая история нужна всем устройствам участников
    return [d for d in devices if (d["created_at"] or "") <= row["created_at"]]


def _acks_for(conn, item_id):
    return {r["device_id"]: r["level"] for r in conn.execute("SELECT device_id, level FROM relay_acks WHERE item_id = ?", (item_id,)).fetchall()}


# ---------------------------------------------------------------- удаление
def _delete_message(conn, kind, row):
    mid = row["id"]
    table = "messages" if kind == "dm" else "space_messages"
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (mid,))
    conn.execute("DELETE FROM message_reactions WHERE message_id = ?", (mid,))
    conn.execute("DELETE FROM message_hidden WHERE message_id = ?", (mid,))
    conn.execute("DELETE FROM relay_acks WHERE item_id = ?", (mid,))


def _maybe_prune_message(conn, kind, row, start):
    targets = _target_devices(conn, kind, row, start)
    if not targets:
        return False
    acks = _acks_for(conn, row["id"])
    if all(acks.get(d["id"], 0) >= 2 for d in targets):
        _delete_message(conn, kind, row)
        return True
    return False


def _event_targets(ev):
    try:
        return json.loads(ev["targets"])
    except (TypeError, ValueError):
        return []


def _maybe_prune_event(conn, ev):
    users = _event_targets(ev)
    devices = [d for d in _devices_of(conn, users) if (d["created_at"] or "") <= ev["created_at"]]
    acks = _acks_for(conn, ev["id"])
    if all(acks.get(d["id"], 0) >= 2 for d in devices):
        conn.execute("DELETE FROM relay_events WHERE id = ?", (ev["id"],))
        conn.execute("DELETE FROM relay_acks WHERE item_id = ?", (ev["id"],))
        return True
    return False


# ---------------------------------------------------------------- выдача устройству
def _device_row(conn, device_id):
    return conn.execute("SELECT id, user_id, created_at FROM devices WHERE id = ? AND token_hash IS NOT NULL", (device_id,)).fetchone()


def pull(user_id, device_id):
    """Что устройству ещё нужно забрать: сообщения и события, которые оно не подтвердило."""
    from app.messages import attach_reactions

    with get_connection() as conn:
        dev = _device_row(conn, device_id)
        if not dev or dev["user_id"] != user_id:
            return None
        start = started_at(conn)
        acked = {r["item_id"] for r in conn.execute("SELECT item_id FROM relay_acks WHERE device_id = ? AND level >= 1", (device_id,)).fetchall()}
        # Сначала только (время, тип, id) — полные строки с голосовыми читаем лишь для первой порции.
        index = [(r["created_at"], "dm", r["id"]) for r in conn.execute(
            "SELECT id, created_at FROM messages WHERE (from_user = ? OR to_user = ?) AND (created_at >= ? OR created_at < ?)",
            (user_id, user_id, dev["created_at"], start)).fetchall() if r["id"] not in acked]
        index += [(r["created_at"], "sp", r["id"]) for r in conn.execute(
            "SELECT id, created_at FROM space_messages WHERE space_id IN (SELECT space_id FROM space_members WHERE user_id = ?) "
            "AND (created_at >= ? OR created_at < ?)", (user_id, dev["created_at"], start)).fetchall() if r["id"] not in acked]
        index.sort()
        hidden = {r["message_id"] for r in conn.execute("SELECT message_id FROM message_hidden WHERE user_id = ?", (user_id,)).fetchall()}
        out, size, more = [], 0, False
        for created, kind, mid in index:
            if len(out) >= MAX_PULL_MESSAGES or (out and size >= MAX_PULL_BYTES):
                more = True
                break
            if kind == "dm":
                row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT m.*, u.display_name AS sender_name, u.username AS sender_username FROM space_messages m "
                    "JOIN users u ON u.id = m.from_user WHERE m.id = ?", (mid,)).fetchone()
            if not row:
                continue
            m = dict(row)
            m["kind"] = kind
            size += len(m.get("audio_data") or "") + len(m.get("text") or "") + 400
            out.append(m)
        for kind in ("dm", "sp"):
            part = [m for m in out if m["kind"] == kind]
            if part:
                attach_reactions(part)
        for m in out:
            m["hidden_me"] = m["id"] in hidden
        # --- события
        ev_rows = conn.execute("SELECT * FROM relay_events WHERE created_at >= ? ORDER BY created_at ASC", (dev["created_at"],)).fetchall()
        events = []
        for e in ev_rows:
            if e["id"] in acked or user_id not in _event_targets(e):
                continue
            if len(events) >= MAX_PULL_EVENTS:
                more = True
                break
            events.append({"id": e["id"], "kind": e["kind"], "actor_id": e["actor_id"], "chat": e["chat"],
                           "payload": json.loads(e["payload"]), "created_at": e["created_at"]})
        return {"messages": out, "events": events, "more": more, "server_time": _now()}


# ---------------------------------------------------------------- подтверждения
def ack(user_id, device_id, ids, level):
    """Устройство подтверждает получение (level 1) / надёжное сохранение (level 2)."""
    level = 2 if int(level or 1) >= 2 else 1
    ids = [str(i) for i in (ids or [])][:MAX_ACK_IDS]
    if not ids:
        return 0
    done = 0
    with get_connection() as conn:
        dev = _device_row(conn, device_id)
        if not dev or dev["user_id"] != user_id:
            return None
        start = started_at(conn)
        now = _now()
        for item_id in ids:
            kind = row = None
            r = conn.execute("SELECT id, from_user, to_user, created_at FROM messages WHERE id = ?", (item_id,)).fetchone()
            if r and user_id in (r["from_user"], r["to_user"]):
                kind, row = "dm", r
            else:
                r = conn.execute("SELECT id, space_id, from_user, created_at FROM space_messages WHERE id = ?", (item_id,)).fetchone()
                if r and user_id in _members(conn, r["space_id"]):
                    kind, row = "sp", r
                else:
                    e = conn.execute("SELECT * FROM relay_events WHERE id = ?", (item_id,)).fetchone()
                    if e and user_id in _event_targets(e):
                        kind, row = "evt", e
            if not kind:
                continue
            cur = conn.execute("UPDATE relay_acks SET level = ?, acked_at = ? WHERE item_id = ? AND device_id = ? AND level < ?",
                               (level, now, item_id, device_id, level))
            if not cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO relay_acks (item_id, device_id, level, acked_at) VALUES (?, ?, ?, ?)",
                             (item_id, device_id, level, now))
            done += 1
            if level >= 2:
                if kind == "evt":
                    _maybe_prune_event(conn, row)
                else:
                    _maybe_prune_message(conn, kind, row, start)
        conn.commit()
    return done


# ---------------------------------------------------------------- события
def _clean_payload(kind, payload):
    p = payload if isinstance(payload, dict) else {}
    mid = str(p.get("message_id", ""))[:64]
    if kind == "react":
        emoji = p.get("emoji")
        emoji = str(emoji)[:16] if emoji else None
        return {"message_id": mid, "emoji": emoji}
    if kind == "pin":
        return {"message_id": mid, "pinned": bool(p.get("pinned"))}
    if kind in ("delete_all", "delete_me", "played"):
        return {"message_id": mid}
    if kind in ("read", "space_read"):
        return {"upto": str(p.get("upto") or _now())[:40]}
    return {}


def post_event(actor_id, device_id, kind, chat, payload):
    """Возвращает (событие, список_пользователей_для_уведомления) или (None, ошибка)."""
    from app import blocks as blocks_mod

    if kind not in EVENT_KINDS:
        return None, "Unknown event kind"
    chat = str(chat or "")
    clean = _clean_payload(kind, payload)
    if kind in ("react", "pin", "delete_all", "delete_me", "played") and not clean["message_id"]:
        return None, "message_id is required"
    with get_connection() as conn:
        if chat.startswith("dm:"):
            other = chat[3:]
            if kind in ("space_read",):
                return None, "Bad chat"
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (other,)).fetchone():
                return None, "User not found"
            targets = {actor_id, other}
            if other != actor_id and (blocks_mod.is_blocked(other, actor_id) or blocks_mod.is_blocked(actor_id, other)):
                targets = {actor_id}
        elif chat.startswith("sp:"):
            space_id = chat[3:]
            members = _members(conn, space_id)
            if actor_id not in members:
                return None, "Not a member"
            if kind in ("read", "played"):
                return None, "Bad chat"
            targets = set(members)
        else:
            return None, "Bad chat"
        if kind in ("delete_me", "space_read"):
            targets = {actor_id}
        ev_id = str(uuid.uuid4())
        created = _now()
        conn.execute(
            "INSERT INTO relay_events (id, kind, actor_id, chat, payload, targets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ev_id, kind, actor_id, chat, json.dumps(clean, ensure_ascii=False), json.dumps(sorted(targets)), created),
        )
        # Устройство-автор уже применило событие у себя — ему подтверждение не нужно.
        conn.execute("INSERT OR IGNORE INTO relay_acks (item_id, device_id, level, acked_at) VALUES (?, ?, 2, ?)", (ev_id, device_id, created))
        # «Удалить у всех»: недоставленную копию на сервере стираем сразу.
        if kind == "delete_all":
            m = conn.execute("SELECT id, from_user, to_user, created_at FROM messages WHERE id = ? AND from_user = ?", (clean["message_id"], actor_id)).fetchone()
            if m:
                _delete_message(conn, "dm", m)
            else:
                m = conn.execute("SELECT id, space_id, from_user, created_at FROM space_messages WHERE id = ? AND from_user = ?", (clean["message_id"], actor_id)).fetchone()
                if m:
                    _delete_message(conn, "sp", m)
        conn.commit()
    ev = {"id": ev_id, "kind": kind, "actor_id": actor_id, "chat": chat, "payload": clean, "created_at": created}
    return ev, sorted(targets)


def _system_event(conn, kind, chat, payload, targets, actor="system"):
    ev_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO relay_events (id, kind, actor_id, chat, payload, targets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ev_id, kind, actor, chat, json.dumps(payload, ensure_ascii=False), json.dumps(sorted(targets)), _now()),
    )
    return ev_id


# ---------------------------------------------------------------- уборка
def sweep(now=None):
    """Удаляет просроченное (по умолчанию 4 дня) и то, что все устройства уже подтвердили.
    Возвращает список пользователей, которым пришло событие «не доставлено»."""
    now = now or _now_dt()
    lifetime = ttl()
    notify = set()
    stats = {"expired": 0, "pruned": 0, "undelivered": 0, "events": 0}
    with get_connection() as conn:
        start = started_at(conn)
        start_dt = _parse(start) or now
        for kind, table in (("dm", "messages"), ("sp", "space_messages")):
            cols = "id, from_user, to_user, created_at" if kind == "dm" else "id, space_id, from_user, created_at"
            rows = conn.execute(f"SELECT {cols} FROM {table}").fetchall()
            for r in rows:
                created = _parse(r["created_at"]) or now
                born = max(created, start_dt)
                if now - born < lifetime:
                    # ещё не просрочено — но вдруг уже всё подтверждено (например, лишнее устройство вышло)
                    if conn.execute("SELECT 1 FROM relay_acks WHERE item_id = ? LIMIT 1", (r["id"],)).fetchone():
                        if _maybe_prune_message(conn, kind, r, start):
                            stats["pruned"] += 1
                    continue
                # просрочено: уведомляем отправителя, только если получатель так и не получил
                if (r["created_at"] or "") >= start and not _legacy_skip(r, kind):
                    acks = _acks_for(conn, r["id"])
                    others = {u for u in _participants(conn, kind, r) if u != r["from_user"]}
                    other_devices = {d["id"] for d in _devices_of(conn, others)}
                    delivered = any(acks.get(did, 0) >= 1 for did in other_devices)
                    if others and not delivered:
                        chat = ("dm:" + r["to_user"]) if kind == "dm" else ("sp:" + r["space_id"])
                        _system_event(conn, "undelivered", chat, {"message_id": r["id"]}, {r["from_user"]})
                        notify.add(r["from_user"])
                        stats["undelivered"] += 1
                _delete_message(conn, kind, r)
                stats["expired"] += 1
        for e in conn.execute("SELECT * FROM relay_events").fetchall():
            created = _parse(e["created_at"]) or now
            if now - created >= lifetime:
                conn.execute("DELETE FROM relay_events WHERE id = ?", (e["id"],))
                conn.execute("DELETE FROM relay_acks WHERE item_id = ?", (e["id"],))
                stats["events"] += 1
            elif conn.execute("SELECT 1 FROM relay_acks WHERE item_id = ? LIMIT 1", (e["id"],)).fetchone():
                if _maybe_prune_event(conn, e):
                    stats["events"] += 1
        conn.execute(
            "DELETE FROM relay_acks WHERE item_id NOT IN (SELECT id FROM messages) "
            "AND item_id NOT IN (SELECT id FROM space_messages) AND item_id NOT IN (SELECT id FROM relay_events)"
        )
        conn.commit()
    return notify, stats


def _legacy_skip(row, kind):
    return kind == "dm" and row["from_user"] == row["to_user"]  # «Избранное» — получателя нет


def sweeper_loop(on_notify=None, interval=60):
    import time

    while True:
        time.sleep(interval)
        try:
            notify, _ = sweep()
            if on_notify and notify:
                on_notify(notify)
        except Exception as exc:  # уборка не должна падать насовсем
            print("RELAY SWEEP ERROR:", exc, flush=True)