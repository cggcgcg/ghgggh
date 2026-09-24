from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json
import socket
import threading
import time

from app.database import init_db
from app.users import create_user, get_user, find_by_username, update_user
from app.messages import create_message, get_conversation, get_conversations
from app.workspace import (
    get_settings, update_settings,
    create_space, get_spaces, get_space, update_space, delete_space,
    get_members, add_member, remove_member, set_member_role, get_member_role,
    can_manage, is_member, join_space, join_by_invite_code, search_public_spaces,
    create_device, get_devices,
)
from app.contacts import add_contact, get_contact_ids, remove_contact
from app import calls


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"


class Handler(BaseHTTPRequestHandler):

    def send_json(self, status, data):
        response = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    # ---- WebSocket (звонки) ----------------------------------------

    def send_ws_json(self, message):
        """Thread-safe: вызывается и из своего же потока (pong), и из ЧУЖОГО
        потока (когда собеседник шлёт нам offer/answer/ice-candidate)."""
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        frame = calls.encode_frame(payload)
        lock = getattr(self, "_ws_write_lock", None)
        if lock is None:
            self.wfile.write(frame)
            return
        with lock:
            self.wfile.write(frame)

    def close_ws(self):
        """Будит поток этого соединения, если он сейчас заблокирован на
        чтении сокета — используется, когда тот же user_id переподключился
        с другой вкладки/устройства."""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _handle_ws_calls(self):
        ws_key = self.headers.get("Sec-WebSocket-Key")
        if not ws_key:
            self.send_json(400, {"error": "Missing Sec-WebSocket-Key"})
            return

        accept = calls.compute_accept_key(ws_key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        self._ws_write_lock = threading.Lock()
        state = {}
        opened_at = time.monotonic()
        frames_seen = 0
        close_reason = "unknown"

        try:
            while True:
                opcode, payload = calls.decode_frame(self.rfile.read)
                if opcode is None:
                    close_reason = "read-returned-none (peer closed / socket dead)"
                    break

                frames_seen += 1

                if opcode == 0x8:  # close
                    close_reason = "close-frame (clean, peer requested)"
                    break

                if opcode == 0x9:  # ping -> pong
                    with self._ws_write_lock:
                        self.wfile.write(calls.encode_frame(payload, opcode=0xA))
                    continue

                if opcode != 0x1:  # интересует только текст (JSON)
                    continue

                try:
                    message = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue

                if isinstance(message, dict):
                    calls.handle_client_message(self, state, message)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            close_reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — любая другая ошибка не должна остаться незамеченной
            close_reason = f"UNEXPECTED {type(exc).__name__}: {exc}"
            raise
        finally:
            lifetime = time.monotonic() - opened_at
            user_id = state.get("user_id")
            print(
                f"[ws][{calls._REPLICA if hasattr(calls, '_REPLICA') else '?'}] "
                f"closed user={user_id} lifetime={lifetime:.1f}s frames={frames_seen} reason={close_reason}",
                flush=True,
            )
            if user_id:
                calls.unregister(user_id, self)

    def send_file(self, file_path):
        try:
            content = file_path.read_bytes()

            self.send_response(200)

            if file_path.suffix == ".html":
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8"
                )
            elif file_path.suffix == ".css":
                self.send_header(
                    "Content-Type",
                    "text/css; charset=utf-8"
                )
            elif file_path.suffix == ".js":
                self.send_header(
                    "Content-Type",
                    "application/javascript; charset=utf-8"
                )
            else:
                self.send_header(
                    "Content-Type",
                    "application/octet-stream"
                )

            self.send_header(
                "Content-Length",
                str(len(content))
            )

            self.end_headers()
            self.wfile.write(content)

        except FileNotFoundError:
            self.send_json(404, {
                "error": "File not found"
            })

    def read_json_body(self):
        try:
            content_length = int(
                self.headers.get("Content-Length", 0)
            )

            if content_length <= 0:
                return None

            body = self.rfile.read(content_length)

            return json.loads(
                body.decode("utf-8")
            )

        except (ValueError, json.JSONDecodeError):
            return None

    # =====================================================
    # GET
    # =====================================================

    def do_GET(self):

        # Апгрейд до WebSocket для сигналинга звонков — держим на том же
        # порту, что и остальной API, чтобы не городить второй порт/прокси.
        if self.path == "/ws/calls" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws_calls()
            return

        # Главная страница TGClone
        if self.path == "/" or self.path == "/index.html":
            self.send_file(
                FRONTEND_DIR / "index.html"
            )
            return

        # Проверка сервера
        if self.path == "/api/health":
            self.send_json(200, {
                "status": "ok",
                "project": "TGClone"
            })
            return

        if self.path.startswith("/api/settings/"):
            user_id = self.path.replace("/api/settings/", "", 1)
            if not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(200, get_settings(user_id))
            return

        # /api/spaces/find/<space_id>       -> публичная карточка пространства (для "join по id")
        # /api/spaces/search/<query>        -> поиск публичных каналов/групп по названию
        # /api/spaces/<space_id>/members    -> список участников
        # /api/spaces/<user_id>             -> список пространств, где user_id состоит участником
        if self.path.startswith("/api/spaces/"):
            remainder = self.path.replace("/api/spaces/", "", 1)
            parts = remainder.split("/")

            if parts[0] == "find" and len(parts) == 2:
                space = get_space(parts[1])
                if not space:
                    self.send_json(404, {"error": "Space not found"})
                elif space["is_private"]:
                    self.send_json(403, {"error": "This space is private"})
                else:
                    self.send_json(200, space)
                return

            if parts[0] == "search" and len(parts) == 2:
                self.send_json(200, search_public_spaces(parts[1]))
                return

            if len(parts) == 2 and parts[1] == "members":
                space_id = parts[0]
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                else:
                    self.send_json(200, get_members(space_id))
                return

            user_id = parts[0]
            self.send_json(200, get_spaces(user_id))
            return

        # Контакты пользователя (добавленные по user id) — отдаём сразу
        # профили, а не голые id, фронту не нужно резолвить их отдельно.
        if self.path.startswith("/api/contacts/"):
            owner_id = self.path.replace("/api/contacts/", "", 1)
            contact_ids = get_contact_ids(owner_id)
            users = []
            for contact_id in contact_ids:
                user = get_user(contact_id)
                if user:
                    users.append(user)
            self.send_json(200, users)
            return

        if self.path.startswith("/api/devices/"):
            user_id = self.path.replace("/api/devices/", "", 1)
            self.send_json(200, get_devices(user_id))
            return

        # История переписки между двумя пользователями
        if self.path.startswith("/api/messages"):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)

            user_a = params.get("user_a", [None])[0]
            user_b = params.get("user_b", [None])[0]

            if not user_a or not user_b:
                self.send_json(400, {
                    "error": "user_a and user_b are required"
                })
                return

            messages = get_conversation(user_a, user_b)
            self.send_json(200, messages)
            return

        # Список всех собеседников пользователя (входящие и исходящие)
        if self.path.startswith("/api/conversations/"):
            user_id = self.path.replace(
                "/api/conversations/",
                "",
                1
            )

            other_ids = get_conversations(user_id)
            users = []
            for other_id in other_ids:
                user = get_user(other_id)
                if user:
                    users.append(user)

            self.send_json(200, users)
            return

        # Поиск пользователя по username
        if self.path.startswith("/api/users/search/"):
            username = self.path.replace(
                "/api/users/search/",
                "",
                1
            )

            user = find_by_username(username)

            if user:
                self.send_json(200, user)
            else:
                self.send_json(404, {
                    "error": "User not found"
                })

            return

        # Получение пользователя по ID
        if self.path.startswith("/api/users/"):
            user_id = self.path.replace(
                "/api/users/",
                "",
                1
            )

            user = get_user(user_id)

            if user:
                self.send_json(200, user)
            else:
                self.send_json(404, {
                    "error": "User not found"
                })

            return

        self.send_json(404, {
            "error": "Not found"
        })

    # =====================================================
    # POST
    # =====================================================

    def do_POST(self):

        # Создание пользователя
        if self.path == "/api/users":

            data = self.read_json_body()

            if not data:
                self.send_json(400, {
                    "error": "Invalid JSON"
                })
                return

            username = str(
                data.get("username", "")
            ).strip()

            display_name = str(
                data.get("display_name", "")
            ).strip()

            # Проверка username
            if not username:
                self.send_json(400, {
                    "error": "Username is required"
                })
                return

            # Проверка имени
            if not display_name:
                self.send_json(400, {
                    "error": "Display name is required"
                })
                return

            # Проверяем, существует ли username
            if find_by_username(username):
                self.send_json(409, {
                    "error": "Username already exists"
                })
                return

            try:
                user = create_user(
                    username,
                    display_name
                )

                self.send_json(201, user)

            except Exception as error:
                print("CREATE USER ERROR:", error)

                self.send_json(500, {
                    "error": "Failed to create user"
                })

            return

        # Отправка сообщения (текст или голосовое)
        if self.path == "/api/messages":

            data = self.read_json_body()

            if not data:
                self.send_json(400, {
                    "error": "Invalid JSON"
                })
                return

            from_user = str(data.get("from_user", "")).strip()
            to_user = str(data.get("to_user", "")).strip()
            text = str(data.get("text", "")).strip()
            msg_type = str(data.get("type", "text")).strip() or "text"
            audio_data = data.get("audio_data")
            waveform = data.get("waveform")

            if not from_user or not to_user:
                self.send_json(400, {
                    "error": "from_user and to_user are required"
                })
                return

            if msg_type in ("voice", "video"):
                if not audio_data:
                    self.send_json(400, {
                        "error": "audio_data is required for media messages"
                    })
                    return
            else:
                if not text:
                    self.send_json(400, {
                        "error": "text is required"
                    })
                    return

            if not get_user(from_user) or not get_user(to_user):
                self.send_json(404, {
                    "error": "User not found"
                })
                return

            try:
                message = create_message(
                    from_user,
                    to_user,
                    text=text,
                    msg_type=msg_type,
                    audio_data=audio_data,
                    waveform=waveform,
                )
                self.send_json(201, message)

            except Exception as error:
                print("CREATE MESSAGE ERROR:", error)
                self.send_json(500, {
                    "error": "Failed to send message"
                })

            return

        if self.path == "/api/spaces":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            name = str(data.get("name", "")).strip()
            kind = str(data.get("kind", "")).strip()
            description = str(data.get("description", "")).strip()
            photo = data.get("photo") or None
            is_private = bool(data.get("is_private"))
            if not owner_id or not name or kind not in ("group", "channel"):
                self.send_json(400, {"error": "owner_id, name and valid kind are required"})
                return
            if not get_user(owner_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(201, create_space(owner_id, name, kind, description=description, photo=photo, is_private=is_private))
            return

        # Вступить в ПУБЛИЧНОЕ пространство, зная его id (найдено как
        # обычный собеседник, см. GET /api/spaces/find/<id>).
        if self.path.startswith("/api/spaces/") and self.path.endswith("/join"):
            space_id = self.path[len("/api/spaces/"):-len("/join")]
            data = self.read_json_body() or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id or not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            space, error = join_space(space_id, user_id)
            if error:
                self.send_json(403 if "private" in error else 404, {"error": error})
                return
            self.send_json(200, space)
            return

        # Вступить по коду приглашения (ссылке) — работает и для приватных,
        # и для публичных пространств.
        if self.path.startswith("/api/spaces/join-code/"):
            invite_code = self.path.replace("/api/spaces/join-code/", "", 1)
            data = self.read_json_body() or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id or not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            space, error = join_by_invite_code(invite_code, user_id)
            if error:
                self.send_json(404, {"error": error})
                return
            self.send_json(200, space)
            return

        # Добавить участника вручную (владелец/админ добавляет кого-то,
        # кто уже состоит в этом канале — по договорённости для приватных
        # пространств это единственный способ привести нового человека,
        # кроме ссылки-приглашения).
        if self.path.startswith("/api/spaces/") and self.path.endswith("/members"):
            space_id = self.path[len("/api/spaces/"):-len("/members")]
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()
            target_id = str(data.get("user_id", "")).strip()
            if not get_space(space_id):
                self.send_json(404, {"error": "Space not found"})
                return
            if not can_manage(space_id, by):
                self.send_json(403, {"error": "Only the owner or an admin can add members"})
                return
            if not target_id or not get_user(target_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(200, add_member(space_id, target_id))
            return

        if self.path.startswith("/api/spaces/") and self.path.endswith("/leave"):
            space_id = self.path[len("/api/spaces/"):-len("/leave")]
            data = self.read_json_body() or {}
            user_id = str(data.get("user_id", "")).strip()
            space = get_space(space_id)
            if not space:
                self.send_json(404, {"error": "Space not found"})
                return
            if space["owner_id"] == user_id:
                self.send_json(400, {"error": "Owner cannot leave — delete the space instead"})
                return
            self.send_json(200, remove_member(space_id, user_id))
            return

        # Добавить контакт по user id.
        if self.path == "/api/contacts":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            contact_user_id = str(data.get("contact_user_id", "")).strip()
            if not owner_id or not contact_user_id:
                self.send_json(400, {"error": "owner_id and contact_user_id are required"})
                return
            if not get_user(owner_id) or not get_user(contact_user_id):
                self.send_json(404, {"error": "User not found"})
                return
            add_contact(owner_id, contact_user_id)
            self.send_json(201, get_user(contact_user_id))
            return

        if self.path == "/api/contacts/remove":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            contact_user_id = str(data.get("contact_user_id", "")).strip()
            remove_contact(owner_id, contact_user_id)
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/devices":
            data = self.read_json_body() or {}
            user_id = str(data.get("user_id", "")).strip()
            name = str(data.get("name", "")).strip() or "Новое устройство"
            if not user_id or not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(201, create_device(user_id, name))
            return

        self.send_json(404, {
            "error": "Not found"
        })

    def do_PUT(self):
        if self.path.startswith("/api/users/"):
            user_id = self.path.replace("/api/users/", "", 1)
            data = self.read_json_body() or {}
            current = get_user(user_id)
            username = str(data.get("username", "")).strip()
            display_name = str(data.get("display_name", "")).strip()
            duplicate = find_by_username(username)
            if not current:
                self.send_json(404, {"error": "User not found"})
            elif not username or not display_name:
                self.send_json(400, {"error": "Username and display_name are required"})
            elif duplicate and duplicate["id"] != user_id:
                self.send_json(409, {"error": "Username already exists"})
            else:
                self.send_json(200, update_user(user_id, username, display_name))
            return

        if self.path.startswith("/api/settings/"):
            user_id = self.path.replace("/api/settings/", "", 1)
            if not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(200, update_settings(user_id, self.read_json_body() or {}))
            return

        # /api/spaces/<id>/members/<user_id>  -> сменить роль (admin/member)
        # /api/spaces/<id>                    -> изменить название/фото/описание/фон
        if self.path.startswith("/api/spaces/"):
            remainder = self.path.replace("/api/spaces/", "", 1)
            parts = remainder.split("/")
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()

            if len(parts) == 3 and parts[1] == "members":
                space_id, _, target_id = parts
                role = str(data.get("role", "")).strip()
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                elif not can_manage(space_id, by):
                    self.send_json(403, {"error": "Only the owner or an admin can change roles"})
                elif role not in ("admin", "member"):
                    self.send_json(400, {"error": "role must be 'admin' or 'member'"})
                elif get_member_role(space_id, target_id) == "owner":
                    self.send_json(400, {"error": "Cannot change the owner's role"})
                else:
                    self.send_json(200, set_member_role(space_id, target_id, role))
                return

            if len(parts) == 1:
                space_id = parts[0]
                space = get_space(space_id)
                if not space:
                    self.send_json(404, {"error": "Space not found"})
                elif not can_manage(space_id, by):
                    self.send_json(403, {"error": "Only the owner or an admin can edit this space"})
                else:
                    self.send_json(200, update_space(space_id, data))
                return

        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        # /api/spaces/<id>/members/<user_id>  -> убрать участника (сам вышел,
        #                                         либо владелец/админ выгнал)
        # /api/spaces/<id>                    -> удалить канал/группу (только владелец)
        # /api/contacts/<owner_id>/<contact_user_id> -> убрать контакт
        if self.path.startswith("/api/spaces/"):
            remainder = self.path.replace("/api/spaces/", "", 1)
            parts = remainder.split("/")
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()

            if len(parts) == 3 and parts[1] == "members":
                space_id, _, target_id = parts
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                    return
                # Разрешено: сам участник выходит (by == target_id),
                # либо это делает владелец/админ.
                if by != target_id and not can_manage(space_id, by):
                    self.send_json(403, {"error": "Not allowed"})
                    return
                if get_member_role(space_id, target_id) == "owner":
                    self.send_json(400, {"error": "Owner cannot be removed — delete the space instead"})
                    return
                self.send_json(200, remove_member(space_id, target_id))
                return

            if len(parts) == 1:
                space_id = parts[0]
                space = get_space(space_id)
                if not space:
                    self.send_json(404, {"error": "Space not found"})
                    return
                if space["owner_id"] != by:
                    self.send_json(403, {"error": "Only the owner can delete this space"})
                    return
                delete_space(space_id)
                self.send_json(200, {"ok": True})
                return

        if self.path.startswith("/api/contacts/"):
            remainder = self.path.replace("/api/contacts/", "", 1)
            parts = remainder.split("/")
            if len(parts) == 2:
                owner_id, contact_user_id = parts
                self.send_json(200, {"contacts": remove_contact(owner_id, contact_user_id)})
                return

        self.send_json(404, {"error": "Not found"})


# =====================================================
# START
# =====================================================

init_db()

import os

PORT = int(os.environ.get("PORT", 8000))

server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    Handler
)

print(
    f"TGClone server started on port {PORT} (replica={os.environ.get('RAILWAY_REPLICA_ID', 'local')[:8]})",
    flush=True,
)

server.serve_forever()