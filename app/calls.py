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
    peer_id = clear_call_pair(user_id)
    if peer_id:
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

    if msg_type == "call-offer":
        if not to_user:
            return
        print(f"[calls][{_REPLICA}] call-offer from={user_id} to={to_user} online_here={list(_clients.keys())}", flush=True)
        if not is_online(to_user):
            handler.send_ws_json({"type": "call-unavailable", "to": to_user})
            return
        existing_peer = _active_calls.get(to_user)
        if existing_peer and existing_peer != user_id:
            handler.send_ws_json({"type": "call-busy", "to": to_user})
            return
        set_call_pair(user_id, to_user)
        send_to(to_user, {
            "type": "call-offer",
            "from": user_id,
            "mode": message.get("mode", "audio"),
            "sdp": message.get("sdp"),
        })
        return

    if msg_type == "call-answer":
        if to_user:
            send_to(to_user, {"type": "call-answer", "from": user_id, "sdp": message.get("sdp")})
        return

    if msg_type == "ice-candidate":
        if to_user:
            send_to(to_user, {"type": "ice-candidate", "from": user_id, "candidate": message.get("candidate")})
        return

    if msg_type in ("call-reject", "call-end", "call-busy"):
        peer_id = clear_call_pair(user_id)
        target = to_user or peer_id
        if target:
            send_to(target, {"type": msg_type, "from": user_id})
        return 