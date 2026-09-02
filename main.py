from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json

from app.database import init_db
from app.users import create_user, get_user, find_by_username
from app.messages import create_message, get_conversation, get_conversations


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
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

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

        # Отправка сообщения
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

            if not from_user or not to_user or not text:
                self.send_json(400, {
                    "error": "from_user, to_user and text are required"
                })
                return

            if not get_user(from_user) or not get_user(to_user):
                self.send_json(404, {
                    "error": "User not found"
                })
                return

            try:
                message = create_message(from_user, to_user, text)
                self.send_json(201, message)

            except Exception as error:
                print("CREATE MESSAGE ERROR:", error)
                self.send_json(500, {
                    "error": "Failed to send message"
                })

            return

        self.send_json(404, {
            "error": "Not found"
        })


# =====================================================
# START
# =====================================================

init_db()

import os

PORT = int(os.environ.get("PORT", 8000))

server = HTTPServer(
    ("0.0.0.0", PORT),
    Handler
)

print(
    f"TGClone server started on port {PORT}"
)

server.serve_forever()