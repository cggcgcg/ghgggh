"""
WebRTC call signaling over a hand-rolled WebSocket, using only the Python
standard library (no extra deps — the rest of the project doesn't use any
either, so this keeps requirements.txt empty).

Media (audio/video) never touches this server: browsers exchange it directly
peer-to-peer via WebRTC. This module only relays the small JSON handshake
messages (offer/answer/ICE candidates/ring/hangup) between the two people in
a call, plus keeps track of who's currently online and who's paired with
whom so a dropped connection can politely end the call on the other side.
"""

import base64
import hashlib
import json
import os
import struct
import threading
import time

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Если в Railway вдруг больше одной реплики — у каждой свой RAILWAY_REPLICA_ID,
# и список подключённых юзеров (_clients ниже) у них РАЗНЫЙ, т.к. это просто
# память процесса. Печатаем его в логи при каждом hello/register, чтобы сразу
# было видно, если два разных пользователя оказались на разных репликах.
_REPLICA = os.environ.get("RAILWAY_REPLICA_ID", "local")[:8]

# user_id -> connection handler currently holding that user's call socket.
_clients = {}
_clients_lock = threading.Lock()

# user_id -> peer_id, for the two people in a ringing/active call.
_active_calls = {}
_calls_lock = threading.Lock()

# caller_id -> {"callee", "mode", "offered_at", "answered_at"} — нужен, чтобы
# после звонка оставить в чате запись «Исходящий/Входящий/Пропущенный · 1:23».
_call_log = {}


def _log_call_start(caller, callee, mode):
    with _calls_lock:
        _call_log[caller] = {
            "callee": callee, "mode": mode if mode == "video" else "audio",
            "offered_at": time.time(), "answered_at": None,
        }


def _log_call_answered(callee, caller):
    with _calls_lock:
        entry = _call_log.get(caller)
        if entry and entry["callee"] == callee and entry["answered_at"] is None:
            entry["answered_at"] = time.time()


def _finalize_call_log(ended_by, peer_id, reason):
    """Закрывает запись о звонке и кладёт её сообщением в переписку.
    reason: 'end' (повесили трубку), 'reject' (отклонил получатель)."""
    with _calls_lock:
        entry = None
        caller = None
        for candidate in (ended_by, peer_id):
            if candidate and candidate in _call_log:
                caller = candidate
                entry = _call_log.pop(candidate)
                break
    if not entry:
        return

    if entry["answered_at"]:
        status = "ended"
        duration = int(time.time() - entry["answered_at"])
    elif reason == "reject":
        status = "declined"
        duration = 0
    else:
        status = "missed"
        duration = 0

    try:
        from app.messages import create_message  # локальный импорт: без циклов
        create_message(
            caller, entry["callee"],
            text=json.dumps({"mode": entry["mode"], "status": status, "duration": duration}),
            msg_type="call",
        )
    except Exception as exc:  # noqa: BLE001 — лог звонка не должен ронять сигналинг
        print(f"[calls][{_REPLICA}] call log failed: {exc!r}", flush=True)


# ---------------------------------------------------------------------------
# Групповые звонки (до GROUP_CALL_MAX человек одновременно).
# Схема «mesh»: каждый участник соединяется напрямую с каждым другим по
# WebRTC, а сервер только пересылает служебные сигналы и помнит, кто в
# комнате. Комната = одна группа (space_id).
# ---------------------------------------------------------------------------
GROUP_CALL_MAX = 6

# space_id -> {"starter", "mode", "started_at", "members": {user_id: joined_at}}
_group_rooms = {}
# user_id -> space_id, чтобы человек не сидел в двух звонках сразу
_user_room = {}
_group_lock = threading.Lock()


def group_room_status(space_id):
    with _group_lock:
        room = _group_rooms.get(space_id)
        if not room:
            return {"active": False, "count": 0, "participants": [], "mode": "audio"}
        return {
            "active": True,
            "count": len(room["members"]),
            "participants": list(room["members"].keys()),
            "mode": room["mode"],
            "started_at": room["started_at"],
            "max": GROUP_CALL_MAX,
        }


def in_group_call(user_id):
    with _group_lock:
        return user_id in _user_room


def _group_send(space_id, message, exclude=None):
    with _group_lock:
        room = _group_rooms.get(space_id)
        targets = [u for u in room["members"] if u != exclude] if room else []
    for uid in targets:
        send_to(uid, message)


def _group_join(handler, user_id, message):
    from app.workspace import get_space, is_member, get_members  # локально: без циклов импорта
    from app.users import get_user

    space_id = str(message.get("space_id", "")).strip()
    mode = "video" if message.get("mode") == "video" else "audio"
    space = get_space(space_id)
    if not space or space["kind"] != "group" or not is_member(space_id, user_id):
        handler.send_ws_json({"type": "gc-error", "space_id": space_id, "reason": "not-allowed"})
        return
    # Начать звонок можно только с правом «calls»; присоединиться к идущему — любой участник.
    from app.workspace import can_do
    if space_id not in _group_rooms and not can_do(space_id, user_id, "calls"):
        handler.send_ws_json({"type": "gc-error", "space_id": space_id, "reason": "not-allowed"})
        return

    with _calls_lock:
        busy_1to1 = user_id in _active_calls
    if busy_1to1:
        handler.send_ws_json({"type": "gc-error", "space_id": space_id, "reason": "busy"})
        return

    is_new = False
    with _group_lock:
        if user_id in _user_room and _user_room[user_id] != space_id:
            handler.send_ws_json({"type": "gc-error", "space_id": space_id, "reason": "busy"})
            return
        room = _group_rooms.get(space_id)
        if room is None:
            room = {"starter": user_id, "mode": mode, "started_at": time.time(), "members": {}}
            _group_rooms[space_id] = room
            is_new = True
        if user_id not in room["members"] and len(room["members"]) >= GROUP_CALL_MAX:
            handler.send_ws_json({"type": "gc-error", "space_id": space_id, "reason": "full", "max": GROUP_CALL_MAX})
            return
        peers = [u for u in room["members"] if u != user_id]
        room["members"][user_id] = time.time()
        room["max_seen"] = max(room.get("max_seen", 0), len(room["members"]))
        _user_room[user_id] = space_id
        room_mode = room["mode"]
        started_at = room["started_at"]

    handler.send_ws_json({
        "type": "gc-joined", "space_id": space_id, "peers": peers,
        "mode": room_mode, "started_at": started_at, "max": GROUP_CALL_MAX,
    })
    _group_send(space_id, {"type": "gc-peer-joined", "space_id": space_id, "user_id": user_id}, exclude=user_id)

    if is_new:
        # звоним остальным участникам группы, кто сейчас онлайн
        user = get_user(user_id) or {}
        for member in get_members(space_id):
            mid = member.get("user_id") or member.get("id")
            if mid and mid != user_id and is_online(mid) and not in_group_call(mid):
                send_to(mid, {
                    "type": "gc-ring", "space_id": space_id, "from": user_id,
                    "from_name": user.get("display_name", ""), "space_name": space["name"], "mode": room_mode,
                })


def _group_leave(user_id, reason="left"):
    with _group_lock:
        space_id = _user_room.pop(user_id, None)
        room = _group_rooms.get(space_id) if space_id else None
        if not room or user_id not in room["members"]:
            return
        del room["members"][user_id]
        remaining = len(room["members"])
        finished = remaining == 0
        if finished:
            _group_rooms.pop(space_id, None)
        # если остался один человек — звонок считается оконченным для него тоже
        solo = None
        if remaining == 1:
            solo = next(iter(room["members"]))
    _group_send(space_id, {"type": "gc-peer-left", "space_id": space_id, "user_id": user_id, "reason": reason})
    if remaining == 1 and solo:
        # одному в комнате делать нечего — закрываем её сразу
        with _group_lock:
            _user_room.pop(solo, None)
            _group_rooms.pop(space_id, None)
        send_to(solo, {"type": "gc-ended", "space_id": space_id})
        finished = True
    if finished:
        _log_group_call(space_id, room)


def _log_group_call(space_id, room):
    """Кладёт в чат группы запись «Групповой звонок · 5 мин · 3 уч.»."""
    try:
        from app.space_messages import create_space_message
        duration = int(time.time() - room["started_at"])
        create_space_message(
            space_id, room["starter"],
            text=json.dumps({"mode": room["mode"], "status": "ended", "duration": duration,
                             "participants": room.get("max_seen", len(room["members"]) or 2)}),
            msg_type="call",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[calls][{_REPLICA}] group call log failed: {exc!r}", flush=True)


def _group_signal(user_id, message):
    space_id = str(message.get("space_id", "")).strip()
    to_user = str(message.get("to", "")).strip()
    with _group_lock:
        room = _group_rooms.get(space_id)
        ok = bool(room and user_id in room["members"] and to_user in room["members"])
    if ok:
        send_to(to_user, {"type": "gc-signal", "space_id": space_id, "from": user_id, "data": message.get("data")})



def compute_accept_key(key):
    digest = hashlib.sha1((key + WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("utf-8")


def encode_frame(payload, opcode=0x1):
    """Build a single unmasked WebSocket frame (server->client frames are
    never masked per the spec)."""
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)  # FIN=1, no fragmentation needed for our small JSON messages
    if length <= 125:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload


def decode_frame(read):
    """Read exactly one WebSocket frame using a blocking `read(n)` callable
    (pass a file object's .read, e.g. self.rfile.read, so anything already
    buffered by the HTTP layer isn't lost). Returns (opcode, payload), or
    (None, None) once the connection is closed / unreadable."""

    def read_exact(n):
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    header = read_exact(2)
    if header is None:
        return None, None

    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        ext = read_exact(2)
        if ext is None:
            return None, None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = read_exact(8)
        if ext is None:
            return None, None
        length = struct.unpack(">Q", ext)[0]

    mask_key = None
    if masked:
        mask_key = read_exact(4)
        if mask_key is None:
            return None, None

    payload = read_exact(length) if length else b""
    if payload is None:
        return None, None

    if masked and mask_key:
        payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))

    return opcode, payload


def register(user_id, handler):
    with _clients_lock:
        _clients[user_id] = handler
    print(f"[calls][{_REPLICA}] register user={user_id} online_now={list(_clients.keys())}", flush=True)


def unregister(user_id, handler):
    with _clients_lock:
        if _clients.get(user_id) is handler:
            del _clients[user_id]
    print(f"[calls][{_REPLICA}] unregister user={user_id} online_now={list(_clients.keys())}", flush=True)
    _group_leave(user_id, reason="disconnected")
    peer_id = clear_call_pair(user_id)
    if peer_id:
        _finalize_call_log(user_id, peer_id, "end")
        send_to(peer_id, {"type": "call-end", "from": user_id, "reason": "disconnected"})


def send_to(user_id, message):
    with _clients_lock:
        handler = _clients.get(user_id)
    if not handler:
        print(f"[calls][{_REPLICA}] send_to user={user_id} FAILED: not registered here (online_here={list(_clients.keys())})", flush=True)
        return False
    try:
        handler.send_ws_json(message)
        print(f"[calls][{_REPLICA}] send_to user={user_id} type={message.get('type')} OK", flush=True)
        return True
    except Exception as exc:
        print(f"[calls][{_REPLICA}] send_to user={user_id} EXCEPTION: {exc!r}", flush=True)
        return False


def is_online(user_id):
    with _clients_lock:
        return user_id in _clients


def set_call_pair(a, b):
    with _calls_lock:
        _active_calls[a] = b
        _active_calls[b] = a


def clear_call_pair(user_id):
    with _calls_lock:
        peer_id = _active_calls.pop(user_id, None)
        if peer_id is not None:
            _active_calls.pop(peer_id, None)
    return peer_id


def handle_client_message(handler, state, message):
    """Processes one decoded JSON signaling message from `handler`'s socket.
    `state` is a small per-connection dict (just holds "user_id" once known)
    that the caller keeps alive for the lifetime of the WebSocket."""

    msg_type = message.get("type")

    if msg_type == "hello":
        user_id = str(message.get("user_id", "")).strip()
        if not user_id:
            return

        # Если у этого user_id уже было открыто соединение (например, юзер
        # обновил вкладку не закрыв старую) — выгоняем старое, иначе звонок
        # может уйти в мёртвый сокет, пока в браузере уже новый.
        with _clients_lock:
            previous = _clients.get(user_id)
        if previous is not None and previous is not handler:
            try:
                previous.send_ws_json({"type": "replaced"})
            except Exception:
                pass
            previous.close_ws()

        register(user_id, handler)
        state["user_id"] = user_id
        handler.send_ws_json({"type": "hello-ack", "user_id": user_id})
        return

    user_id = state.get("user_id")
    if not user_id:
        return  # ничего не принимаем от незалогиненного сокета

    to_user = str(message.get("to", "")).strip()

    if msg_type == "gc-join":
        _group_join(handler, user_id, message)
        return
    if msg_type == "gc-leave":
        _group_leave(user_id)
        return
    if msg_type == "gc-signal":
        _group_signal(user_id, message)
        return

    if msg_type == "call-offer":
        if not to_user:
            return
        print(f"[calls][{_REPLICA}] call-offer from={user_id} to={to_user} online_here={list(_clients.keys())}", flush=True)
        if not is_online(to_user):
            handler.send_ws_json({"type": "call-unavailable", "to": to_user})
            return
        existing_peer = _active_calls.get(to_user)
        if in_group_call(to_user) or (existing_peer and existing_peer != user_id):
            handler.send_ws_json({"type": "call-busy", "to": to_user})
            return
        set_call_pair(user_id, to_user)
        _log_call_start(user_id, to_user, message.get("mode", "audio"))
        send_to(to_user, {
            "type": "call-offer",
            "from": user_id,
            "mode": message.get("mode", "audio"),
            "sdp": message.get("sdp"),
        })
        return

    if msg_type == "call-answer":
        if to_user:
            _log_call_answered(user_id, to_user)
            send_to(to_user, {"type": "call-answer", "from": user_id, "sdp": message.get("sdp")})
        return

    if msg_type == "ice-candidate":
        if to_user:
            send_to(to_user, {"type": "ice-candidate", "from": user_id, "candidate": message.get("candidate")})
        return

    if msg_type in ("call-reject", "call-end", "call-busy"):
        peer_id = clear_call_pair(user_id)
        if peer_id and msg_type != "call-busy":
            _finalize_call_log(user_id, peer_id, "reject" if msg_type == "call-reject" else "end")
        target = to_user or peer_id
        if target:
            send_to(target, {"type": msg_type, "from": user_id})
        return