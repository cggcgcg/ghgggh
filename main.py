from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json
import socket
import threading

from app.database import init_db
from app.users import create_user, get_user, find_by_username, update_user
from app.messages import create_message, get_conversation, get_conversations
from app.workspace import get_settings, update_settings, create_space, get_spaces, create_device, get_devices
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

        try:
            while True:
                opcode, payload = calls.decode_frame(self.rfile.read)
                if opcode is None:
                    break

                if opcode == 0x8:  # close
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
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            user_id = state.get("user_id")
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

        if self.path.startswith("/api/spaces/"):
            user_id = self.path.replace("/api/spaces/", "", 1)
            self.send_json(200, get_spaces(user_id))
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
            if not owner_id or not name or kind not in ("group", "channel"):
                self.send_json(400, {"error": "owner_id, name and valid kind are required"})
                return
            if not get_user(owner_id):
                self.send_json(404, {"error": "User not found"})
                return
            self.send_json(201, create_space(owner_id, name, kind))
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
    f"TGClone server started on port {PORT}"
)

server.serve_forever()