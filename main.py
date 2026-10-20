from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json
import socket
import threading
import time

from app.database import init_db
from app.users import create_user, get_user, find_by_username, update_user
from app.messages import (
    create_message, get_conversation, get_conversations,
    mark_read, mark_played, set_pinned, delete_message,
)
from app.workspace import (
    get_settings, update_settings,
    create_space, get_spaces, get_space, update_space, set_privacy, delete_space,
    get_members, add_member, remove_member, set_member_role, get_member_role,
    can_manage, is_member, join_space, search_public_spaces,
    can_do, set_permissions, set_member_muted,
    create_device, get_devices,
)
from app.contacts import add_contact, get_contact_ids, remove_contact, get_contact_aliases, set_contact_alias
from app.blocks import block_user, unblock_user, is_blocked, get_blocked_ids
APP_VERSION = "v15-gcall"

from app import workspace as invites
from app import messages as messages_mod
from app import workspace as pins_mod
from app import blocks as blocks_mod
try:
    from app import web_assets          # PWA и страницы правил (необязательный файл)
except ImportError:
    web_assets = None
from app import users as users_mod
from app.space_messages import (
    create_space_message, get_space_messages,
    set_space_pinned, delete_space_message, get_space_message,
)
from app import calls


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"


class Handler(BaseHTTPRequestHandler):

    # По умолчанию BaseHTTPRequestHandler отвечает как HTTP/1.0 — а апгрейд
    # до WebSocket (RFC 6455) требует именно HTTP/1.1. Локально (браузер
    # соединяется с сервером напрямую) это сходило с рук, но через прокси
    # Railway такой хендшейк рвался почти сразу после открытия — сервер
    # писал в лог "closed ... lifetime=0.2s frames=0 reason=read-returned-none"
    # ещё до того, как долетал самый первый ws-фрейм ("hello") от клиента.
    protocol_version = "HTTP/1.1"

    def send_bytes(self, status, body, content_type, cache="no-cache"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self._status = status
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

    def _admin_guard(self, user_id):
        """Админ-доступ = аккаунт из ADMIN_IDS/ADMIN_USERNAMES + секретный ключ
        ADMIN_KEY в заголовке X-Admin-Key (в адресную строку/логи ключ не попадает)."""
        if not blocks_mod.is_admin(user_id):
            self.send_json(403, {"error": "Только для администратора"})
            return False
        ok, error, status = blocks_mod.check_admin_key(self.headers.get("X-Admin-Key", ""))
        if not ok:
            self.send_json(status, {"error": error})
            return False
        return True

    def handle_one_request(self):
        # Один обработчик обслуживает несколько запросов по одному соединению —
        # кэш тела запроса надо сбрасывать перед каждым новым запросом.
        self.__dict__.pop("_json_cache", None)
        self._status = 0
        super().handle_one_request()

    def read_json_body(self):
        if hasattr(self, "_json_cache"):
            return self._json_cache
        self._json_cache = self._read_json_body_raw()
        return self._json_cache

    def _read_json_body_raw(self):
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

        # Главная страница Mijer Chat
        if self.path.split("?")[0] in ("/", "/index.html"):
            self.send_file(
                FRONTEND_DIR / "index.html"
            )
            return

        # ---- установка как приложение (PWA) и страницы правил ----
        route = self.path.split("?")[0]
        if web_assets:
            if route == "/manifest.webmanifest":
                self.send_bytes(200, web_assets.manifest().encode("utf-8"), "application/manifest+json; charset=utf-8")
                return
            if route == "/sw.js":
                self.send_bytes(200, web_assets.SERVICE_WORKER.encode("utf-8"), "application/javascript; charset=utf-8", "no-cache")
                return
            if route in ("/icon-192.png", "/icon-512.png"):
                self.send_bytes(200, web_assets.icon(512 if "512" in route else 192), "image/png", "public, max-age=86400")
                return
            if route in ("/terms", "/privacy", "/delete-account"):
                self.send_bytes(200, web_assets.page(route[1:]), "text/html; charset=utf-8")
                return

        # ---- «печатает…»: /api/activity/<viewer>/<chat_id> ----
        if route.startswith("/api/activity/"):
            parts = route[len("/api/activity/"):].split("/")
            if len(parts) == 2:
                viewer, chat_id = parts
                if get_space(chat_id):
                    acts = users_mod.get_activity(chat_id, exclude_user=viewer)
                else:
                    acts = [a for a in users_mod.get_activity(viewer) if a["user_id"] == chat_id]
                for a in acts:
                    u = get_user(a["user_id"])
                    a["name"] = u["display_name"] if u else ""
                self.send_json(200, acts)
                return

        # ---- закреплённые чаты: /api/pinned-chats/<user_id> ----
        if route.startswith("/api/pinned-chats/"):
            self.send_json(200, pins_mod.list_pinned(route[len("/api/pinned-chats/"):]))
            return

        # ---- непрочитанные: /api/unread/<user_id> ----
        if route.startswith("/api/unread/"):
            self.send_json(200, messages_mod.unread_summary(route[len("/api/unread/"):]))
            return

        # ---- админ ----
        if route == "/api/admin/check":
            q = parse_qs(urlsplit(self.path).query)
            self.send_json(200, {"is_admin": blocks_mod.is_admin((q.get("user") or [""])[0])})
            return
        if route == "/api/admin/reports":
            q = parse_qs(urlsplit(self.path).query)
            if not self._admin_guard((q.get("admin") or [""])[0]):
                return
            self.send_json(200, blocks_mod.list_reports_for_admin())
            return

        # Проверка сервера
        if self.path == "/api/ice-config":
            self.send_json(200, {"iceServers": calls.ice_servers()})
            return

        if self.path == "/api/health":
            self.send_json(200, {
                "status": "ok",
                "project": "Mijer Chat",
                "version": APP_VERSION
            })
            return

        # Идёт ли сейчас групповой звонок в этой группе (для кнопки «Присоединиться»)
        if self.path.startswith("/api/group-call/"):
            space_id = self.path.replace("/api/group-call/", "", 1).split("?")[0]
            self.send_json(200, calls.group_room_status(space_id))
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
        # Статус «в сети»: /api/presence/<user_id>?viewer=<id>
        if self.path.startswith("/api/presence/"):
            target = self.path.replace("/api/presence/", "", 1).split("?")[0]
            q = parse_qs(urlsplit(self.path).query)
            info = users_mod.get_presence(target, (q.get("viewer") or [""])[0])
            if info:
                self.send_json(200, info)
            else:
                self.send_json(404, {"error": "User not found"})
            return

        # Предпросмотр ссылки-приглашения: /api/invites/<code>
        if self.path.startswith("/api/invites/"):
            code = self.path.replace("/api/invites/", "", 1).split("?")[0]
            info = invites.preview(code)
            if info:
                self.send_json(200, info)
            else:
                self.send_json(404, {"error": "Ссылка не найдена"})
            return

        if self.path.startswith("/api/spaces/"):
            remainder = self.path.replace("/api/spaces/", "", 1).split("?")[0]
            parts = remainder.split("/")

            if len(parts) == 2 and parts[1] == "invites":
                q = parse_qs(urlsplit(self.path).query)
                by = (q.get("by") or [""])[0]
                space_id = parts[0]
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                elif not can_do(space_id, by, "invite_link"):
                    self.send_json(403, {"error": "Нет права управлять ссылками"})
                else:
                    self.send_json(200, invites.list_invites(space_id))
                return

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
        # Плюс личный никнейм (alias), если задан — виден только owner_id.
        if self.path.startswith("/api/contacts/"):
            owner_id = self.path.replace("/api/contacts/", "", 1)
            contact_ids = get_contact_ids(owner_id)
            aliases = get_contact_aliases(owner_id)
            users = []
            for contact_id in contact_ids:
                user = get_user(contact_id)
                if user:
                    user = dict(user)
                    user["alias"] = aliases.get(contact_id)
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

            viewer = params.get("viewer", [None])[0]
            messages = get_conversation(user_a, user_b, viewer=viewer)
            self.send_json(200, messages)
            return

        # История сообщений внутри канала/группы
        if self.path.startswith("/api/space-messages/"):
            parsed = urlsplit(self.path)
            space_id = parsed.path.replace("/api/space-messages/", "", 1)
            viewer = parse_qs(parsed.query).get("viewer", [None])[0]
            if not get_space(space_id):
                self.send_json(404, {"error": "Space not found"})
                return
            self.send_json(200, get_space_messages(space_id, viewer=viewer))
            return

        # Список пользователей, которых заблокировал user_id
        if self.path.startswith("/api/blocks/"):
            user_id = self.path.replace("/api/blocks/", "", 1)
            blocked_ids = get_blocked_ids(user_id)
            users = []
            for blocked_id in blocked_ids:
                user = get_user(blocked_id)
                if user:
                    users.append(user)
            self.send_json(200, users)
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

    # ---- мгновенные обновления: сервер «пинает» нужных людей по WebSocket,
    # а страница сразу перезагружает чат (вместо опроса раз в 0,7 с) ----
    def _sync_targets(self):
        path = self.path.split("?")[0]
        data = self.read_json_body() or {}
        if not isinstance(data, dict):
            return None
        g = lambda k: str(data.get(k, "")).strip()
        if path == "/api/messages":
            return "msg", [g("to_user")]
        if path == "/api/space-messages":
            return "msg", [m["user_id"] for m in get_members(g("space_id")) if m["user_id"] != g("from_user")]
        if path == "/api/messages/read":
            return "msg", [g("other_id")]
        if path in ("/api/messages/played", "/api/messages/pin", "/api/messages/delete", "/api/reactions"):
            m = messages_mod.get_message(g("message_id"))
            if m:
                return "msg", [u for u in (m["from_user"], m["to_user"]) if u != g("user_id")]
            sm = get_space_message(g("message_id"))
            if sm:
                return "msg", [x["user_id"] for x in get_members(sm["space_id"]) if x["user_id"] != g("user_id")]
            return None
        if path in ("/api/space-messages/pin", "/api/space-messages/delete"):
            sm = get_space_message(g("message_id"))
            if sm:
                return "msg", [x["user_id"] for x in get_members(sm["space_id"]) if x["user_id"] != g("user_id")]
            return None
        if path == "/api/activity":
            chat = g("chat_id")
            if get_space(chat):
                return "activity", [m["user_id"] for m in get_members(chat) if m["user_id"] != g("user_id")]
            return "activity", [chat]
        return None

    def do_POST(self):
        self._status = 0
        try:
            sync = self._sync_targets()
        except Exception as error:
            print("SYNC TARGETS ERROR:", error)
            sync = None
        self._do_post_inner()
        if sync and 200 <= self._status < 300:
            for uid in set(sync[1]):
                if uid:
                    calls.push(uid, {"type": "sync", "scope": sync[0]})

    def _do_post_inner(self):

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

            if not data.get("accepted_terms"):
                self.send_json(400, {"error": "Нужно принять правила и политику конфиденциальности"})
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
                if data.get("accepted_terms"):
                    users_mod.accept_terms(user["id"])

                self.send_json(201, user)

            except Exception as error:
                print("CREATE USER ERROR:", error)

                self.send_json(500, {
                    "error": "Failed to create user"
                })

            return

        # Отправка сообщения (текст или голосовое)
        # ---- закрепить чат: {user_id, chat_id, pinned} ----
        if self.path == "/api/pinned-chats":
            data = self.read_json_body() or {}
            uid, cid = str(data.get("user_id", "")).strip(), str(data.get("chat_id", "")).strip()
            if not get_user(uid) or not cid:
                self.send_json(400, {"error": "user_id and chat_id are required"})
                return
            result, error = pins_mod.set_pinned(uid, cid, bool(data.get("pinned", True)))
            if error:
                self.send_json(400, {"error": error, "pinned": result})
            else:
                self.send_json(200, result)
            return

        # ---- активность («печатает…»): {user_id, chat_id, kind} ----
        if self.path == "/api/activity":
            data = self.read_json_body() or {}
            users_mod.set_activity(str(data.get("user_id", "")), str(data.get("chat_id", "")), str(data.get("kind", "none")))
            self.send_json(200, {"ok": True})
            return

        # ---- прочитано в группе ----
        if self.path == "/api/space-read":
            data = self.read_json_body() or {}
            uid, sid = str(data.get("user_id", "")), str(data.get("space_id", ""))
            if is_member(sid, uid):
                messages_mod.mark_space_read(sid, uid)
            self.send_json(200, {"ok": True})
            return

        # ---- реакция: {message_id, user_id, emoji} ----
        if self.path == "/api/reactions":
            data = self.read_json_body() or {}
            mid, uid = str(data.get("message_id", "")), str(data.get("user_id", ""))
            msg = messages_mod.get_message(mid)
            smsg = None if msg else get_space_message(mid)
            allowed = (msg and uid in (msg["from_user"], msg["to_user"])) or (smsg and is_member(smsg["space_id"], uid))
            if not allowed:
                self.send_json(404, {"error": "Message not found"})
                return
            messages_mod.set_reaction(mid, uid, str(data.get("emoji", "")))
            self.send_json(200, {"ok": True})
            return

        # ---- жалоба: {reporter_id, target_user_id?, message_id?, reason, details?} ----
        if self.path == "/api/reports":
            data = self.read_json_body() or {}
            rid = str(data.get("reporter_id", "")).strip()
            if not get_user(rid):
                self.send_json(404, {"error": "User not found"})
                return
            result, error = blocks_mod.create_report(
                rid, str(data.get("target_user_id", "")).strip() or None, str(data.get("reason", "")),
                str(data.get("details", "")), str(data.get("message_id", "")).strip() or None)
            if error:
                self.send_json(400, {"error": error})
            else:
                self.send_json(201, result)
            return

        # ---- решение администратора: {admin, target_user_id, action: dismiss|ban|unban} ----
        if self.path == "/api/admin/resolve":
            data = self.read_json_body() or {}
            if not self._admin_guard(str(data.get("admin", ""))):
                return
            action = str(data.get("action", ""))
            if action not in ("dismiss", "ban", "unban"):
                self.send_json(400, {"error": "action: dismiss | ban | unban"})
                return
            target = str(data.get("target_user_id", ""))
            if blocks_mod.is_admin(target) and action == "ban":
                self.send_json(400, {"error": "Нельзя заблокировать администратора"})
                return
            blocks_mod.resolve_reports(target, action)
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/messages":

            data = self.read_json_body()

            if not data:
                self.send_json(400, {
                    "error": "Invalid JSON"
                })
                return

            from_user = str(data.get("from_user", "")).strip()
            to_user = str(data.get("to_user", "")).strip()
            if users_mod.is_banned(from_user):
                self.send_json(403, {"error": "Ваш аккаунт заблокирован за нарушение правил"})
                return
            text = str(data.get("text", "")).strip()
            msg_type = str(data.get("type", "text")).strip() or "text"
            audio_data = data.get("audio_data")
            waveform = data.get("waveform")
            reply_to = data.get("reply_to") or None
            forwarded_from = data.get("forwarded_from") or None

            if not from_user or not to_user:
                self.send_json(400, {
                    "error": "from_user and to_user are required"
                })
                return

            if msg_type in ("voice", "video", "image", "file"):
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

            if is_blocked(to_user, from_user):
                self.send_json(403, {
                    "error": "You are blocked by this user"
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
                    reply_to=reply_to,
                    forwarded_from=forwarded_from,
                )
                self.send_json(201, message)

            except Exception as error:
                print("CREATE MESSAGE ERROR:", error)
                self.send_json(500, {
                    "error": "Failed to send message"
                })

            return

        # ---- статусы / закрепление / удаление личных сообщений ----
        if self.path == "/api/messages/read":
            data = self.read_json_body() or {}
            reader_id = str(data.get("reader_id", "")).strip()
            other_id = str(data.get("other_id", "")).strip()
            if not reader_id or not other_id:
                self.send_json(400, {"error": "reader_id and other_id are required"})
                return
            self.send_json(200, {"updated": mark_read(reader_id, other_id)})
            return

        if self.path == "/api/messages/played":
            data = self.read_json_body() or {}
            message_id = str(data.get("message_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            self.send_json(200, {"ok": mark_played(message_id, user_id)})
            return

        if self.path == "/api/messages/pin":
            data = self.read_json_body() or {}
            message_id = str(data.get("message_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            msg = set_pinned(message_id, user_id, bool(data.get("pinned", True)))
            if not msg:
                self.send_json(404, {"error": "Message not found"})
                return
            self.send_json(200, msg)
            return

        if self.path == "/api/messages/delete":
            data = self.read_json_body() or {}
            message_id = str(data.get("message_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            ok, error = delete_message(message_id, user_id, bool(data.get("for_everyone")))
            if not ok:
                self.send_json(403 if error and "только у себя" in error else 404, {"error": error})
                return
            self.send_json(200, {"ok": True})
            return

        # ---- закрепление / удаление сообщений в группах и каналах ----
        if self.path in ("/api/space-messages/pin", "/api/space-messages/delete"):
            data = self.read_json_body() or {}
            message_id = str(data.get("message_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            msg = get_space_message(message_id)
            if not msg or not is_member(msg["space_id"], user_id):
                self.send_json(404, {"error": "Message not found"})
                return
            manager = can_manage(msg["space_id"], user_id)
            if self.path.endswith("/pin"):
                if not can_do(msg["space_id"], user_id, "pin"):
                    self.send_json(403, {"error": "Нет права закреплять сообщения"})
                    return
                self.send_json(200, set_space_pinned(message_id, bool(data.get("pinned", True))))
                return
            ok, error = delete_space_message(message_id, user_id, bool(data.get("for_everyone")), manager)
            if not ok:
                self.send_json(403, {"error": error})
                return
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/spaces":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            name = str(data.get("name", "")).strip()
            kind = str(data.get("kind", "")).strip()
            description = str(data.get("description", "")).strip()
            photo = data.get("photo") or None
            is_private = bool(data.get("is_private"))
            if not owner_id or not name or kind != "group":  # каналы убраны — остались только группы
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

        # Добавить участника вручную по username (владелец/админ добавляет
        # кого-то, кто уже существует в системе — для приватных пространств
        # это единственный способ привести нового человека).
        if self.path.startswith("/api/spaces/") and self.path.endswith("/members"):
            space_id = self.path[len("/api/spaces/"):-len("/members")]
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()
            username = str(data.get("username", "")).strip()
            if not get_space(space_id):
                self.send_json(404, {"error": "Space not found"})
                return
            if not can_do(space_id, by, "add_members"):
                self.send_json(403, {"error": "Нет права добавлять участников"})
                return
            target = find_by_username(username) if username else None
            if not target:
                self.send_json(404, {"error": "Пользователь с таким username не найден"})
                return
            self.send_json(200, add_member(space_id, target["id"]))
            return

        # Пульс присутствия: {user_id, hide_status?}
        if self.path == "/api/presence":
            data = self.read_json_body() or {}
            uid = str(data.get("user_id", "")).strip()
            if not get_user(uid):
                self.send_json(404, {"error": "User not found"})
                return
            if "status_visibility" in data:
                if not users_mod.set_status_visibility(uid, str(data.get("status_visibility"))):
                    self.send_json(400, {"error": "status_visibility: everyone | contacts | nobody"})
                    return
            users_mod.touch_user(uid)
            self.send_json(200, users_mod.get_presence(uid, uid))
            return

        # Создать ссылку-приглашение: {by, hours?, max_uses?}
        if self.path.startswith("/api/spaces/") and self.path.endswith("/invites"):
            space_id = self.path[len("/api/spaces/"):-len("/invites")]
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()
            if not get_space(space_id):
                self.send_json(404, {"error": "Space not found"})
                return
            if not can_do(space_id, by, "invite_link"):
                self.send_json(403, {"error": "Нет права создавать ссылки"})
                return
            try:
                hours = float(data["hours"]) if data.get("hours") else None
                max_uses = int(data["max_uses"]) if data.get("max_uses") else None
            except (TypeError, ValueError):
                self.send_json(400, {"error": "Неверные параметры"})
                return
            if (hours is not None and not 0 < hours <= 24 * 365) or (max_uses is not None and not 0 < max_uses <= 100000):
                self.send_json(400, {"error": "Неверные параметры"})
                return
            self.send_json(201, invites.create_invite(space_id, by, hours, max_uses))
            return

        # Вступить по ссылке: {user_id}
        if self.path.startswith("/api/invites/") and self.path.endswith("/join"):
            code = self.path[len("/api/invites/"):-len("/join")]
            data = self.read_json_body() or {}
            user_id = str(data.get("user_id", "")).strip()
            if not get_user(user_id):
                self.send_json(404, {"error": "User not found"})
                return
            space, error = invites.use_invite(code, user_id)
            if error:
                self.send_json(400, {"error": error})
            else:
                self.send_json(200, space)
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

        # Добавить контакт по username (тому самому @тегу, что в профиле).
        if self.path == "/api/contacts":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            username = str(data.get("username", "")).strip()
            if not owner_id or not username:
                self.send_json(400, {"error": "owner_id and username are required"})
                return
            if not get_user(owner_id):
                self.send_json(404, {"error": "User not found"})
                return
            target = find_by_username(username)
            if not target:
                self.send_json(404, {"error": "Пользователь с таким username не найден"})
                return
            add_contact(owner_id, target["id"])
            self.send_json(201, target)
            return

        if self.path == "/api/contacts/remove":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            contact_user_id = str(data.get("contact_user_id", "")).strip()
            remove_contact(owner_id, contact_user_id)
            self.send_json(200, {"ok": True})
            return

        # Личный никнейм контакта — задаёт/меняет только владелец списка,
        # собеседник об этом не узнаёт и его настоящее имя не меняется.
        if self.path == "/api/contacts/alias":
            data = self.read_json_body() or {}
            owner_id = str(data.get("owner_id", "")).strip()
            contact_user_id = str(data.get("contact_user_id", "")).strip()
            alias = str(data.get("alias", "")).strip()
            if not owner_id or not contact_user_id:
                self.send_json(400, {"error": "owner_id and contact_user_id are required"})
                return
            set_contact_alias(owner_id, contact_user_id, alias)
            self.send_json(200, {"ok": True, "alias": alias or None})
            return

        # Заблокировать пользователя (по username — как и всё остальное).
        # Заблокированный больше не может писать/звонить тому, кто его
        # заблокировал; в обратную сторону ограничений нет.
        if self.path == "/api/blocks":
            data = self.read_json_body() or {}
            blocker_id = str(data.get("blocker_id", "")).strip()
            blocked_username = str(data.get("username", "")).strip().lstrip("@")
            if not blocker_id or not blocked_username:
                self.send_json(400, {"error": "blocker_id and username are required"})
                return
            target = find_by_username(blocked_username)
            if not target:
                self.send_json(404, {"error": "User not found"})
                return
            block_user(blocker_id, target["id"])
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/blocks/remove":
            data = self.read_json_body() or {}
            blocker_id = str(data.get("blocker_id", "")).strip()
            blocked_id = str(data.get("blocked_id", "")).strip()
            unblock_user(blocker_id, blocked_id)
            self.send_json(200, {"ok": True})
            return

        # Отправка сообщения внутри канала/группы. В группе может писать
        # любой участник; в канале — только владелец/админ.
        if self.path == "/api/space-messages":
            data = self.read_json_body() or {}
            space_id = str(data.get("space_id", "")).strip()
            from_user = str(data.get("from_user", "")).strip()
            text = str(data.get("text", "")).strip()
            msg_type = str(data.get("type", "text")).strip() or "text"
            audio_data = data.get("audio_data")
            waveform = data.get("waveform")
            reply_to = data.get("reply_to") or None
            forwarded_from = data.get("forwarded_from") or None

            space = get_space(space_id)
            if not space:
                self.send_json(404, {"error": "Space not found"})
                return
            if not get_user(from_user):
                self.send_json(404, {"error": "User not found"})
                return
            if users_mod.is_banned(from_user):
                self.send_json(403, {"error": "Ваш аккаунт заблокирован за нарушение правил"})
                return
            if not is_member(space_id, from_user):
                self.send_json(403, {"error": "Not a member of this space"})
                return
            need = "media" if msg_type in ("voice", "video", "image", "file") else "send"
            if not can_do(space_id, from_user, need):
                self.send_json(403, {"error": "Вам запрещено отправлять сообщения в этой группе" if need == "send" else "Вам запрещено отправлять медиа в этой группе"})
                return

            if msg_type in ("voice", "video", "image", "file"):
                if not audio_data:
                    self.send_json(400, {"error": "audio_data is required for media messages"})
                    return
            elif not text:
                self.send_json(400, {"error": "text is required"})
                return

            try:
                message = create_space_message(space_id, from_user, text=text, msg_type=msg_type, audio_data=audio_data, waveform=waveform, reply_to=reply_to, forwarded_from=forwarded_from)
                self.send_json(201, message)
            except Exception as error:
                print("CREATE SPACE MESSAGE ERROR:", error)
                self.send_json(500, {"error": "Failed to send message"})
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
            remainder = self.path.replace("/api/spaces/", "", 1).split("?")[0]
            parts = remainder.split("/")
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()

            if len(parts) == 2 and parts[1] == "permissions":
                space_id = parts[0]
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                elif not can_manage(space_id, by):
                    self.send_json(403, {"error": "Только владелец или админ меняет права"})
                else:
                    set_permissions(space_id, data.get("permissions") or {})
                    self.send_json(200, get_space(space_id))
                return

            if len(parts) == 4 and parts[1] == "members" and parts[3] == "mute":
                space_id, target_id = parts[0], parts[2]
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                elif not can_manage(space_id, by):
                    self.send_json(403, {"error": "Только владелец или админ может заглушать"})
                elif get_member_role(space_id, target_id) in (None, "owner", "admin"):
                    self.send_json(400, {"error": "Нельзя заглушить этого участника"})
                else:
                    self.send_json(200, set_member_muted(space_id, target_id, bool(data.get("muted", True))))
                return

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
                    updated = update_space(space_id, data)
                    if "is_private" in data:
                        updated = set_privacy(space_id, bool(data.get("is_private")))
                    self.send_json(200, updated)
                return

        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        # Удаление аккаунта: DELETE /api/users/<id>  тело: {confirm: "<username>"}
        if self.path.startswith("/api/users/"):
            uid = self.path[len("/api/users/"):].split("?")[0]
            data = self.read_json_body() or {}
            user = get_user(uid)
            if not user:
                self.send_json(404, {"error": "User not found"})
            elif str(data.get("confirm", "")).strip().lower() != user["username"].lower():
                self.send_json(400, {"error": "Введите свой username для подтверждения"})
            else:
                users_mod.delete_account(uid)
                self.send_json(200, {"ok": True})
            return

        # /api/spaces/<id>/members/<user_id>  -> убрать участника (сам вышел,
        #                                         либо владелец/админ выгнал)
        # /api/spaces/<id>                    -> удалить канал/группу (только владелец)
        # /api/contacts/<owner_id>/<contact_user_id> -> убрать контакт
        if self.path.startswith("/api/spaces/"):
            remainder = self.path.replace("/api/spaces/", "", 1).split("?")[0]
            parts = remainder.split("/")
            data = self.read_json_body() or {}
            by = str(data.get("by", "")).strip()

            if len(parts) == 3 and parts[1] == "invites":
                space_id, code = parts[0], parts[2]
                q = parse_qs(urlsplit(self.path).query)
                by = (q.get("by") or [""])[0] or by
                if not get_space(space_id):
                    self.send_json(404, {"error": "Space not found"})
                elif not can_do(space_id, by, "invite_link"):
                    self.send_json(403, {"error": "Нет права"})
                else:
                    invites.revoke_invite(space_id, code)
                    self.send_json(200, {"ok": True})
                return

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
    f"Mijer Chat server started on port {PORT} (replica={os.environ.get('RAILWAY_REPLICA_ID', 'local')[:8]})",
    flush=True,
)

server.serve_forever()