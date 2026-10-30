"""Вход по коду из письма.

Письма уходят через Brevo (HTTPS API) — на Railway SMTP на бесплатных
тарифах закрыт, а HTTPS работает везде. Только стандартная библиотека.

Переменные окружения:
  BREVO_API_KEY       — API-ключ Brevo (обязательно)
  BREVO_SENDER_EMAIL  — подтверждённая в Brevo почта отправителя (обязательно)
  BREVO_SENDER_NAME   — имя отправителя (по умолчанию «Mijer Chat»)
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

from app.database import get_connection

CODE_TTL = 10 * 60            # код живёт 10 минут
TICKET_TTL = 15 * 60          # после подтверждения есть 15 минут на регистрацию
MAX_ATTEMPTS = 5              # неверных попыток на один код
RESEND_COOLDOWN = 60          # секунд между отправками на одну почту
MAX_SENDS_PER_HOUR = 5        # писем в час на одну почту
MAX_SENDS_PER_IP_HOUR = 20    # писем в час с одного IP

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_ip_hits = {}
_ip_lock = threading.Lock()


def configured():
    return bool(os.environ.get("BREVO_API_KEY", "").strip()
                and os.environ.get("BREVO_SENDER_EMAIL", "").strip())


def normalize_email(value):
    email = str(value or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return None
    return email


def _hash(*parts):
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def _ip_allowed(ip):
    now = time.time()
    with _ip_lock:
        hits = [t for t in _ip_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= MAX_SENDS_PER_IP_HOUR:
            _ip_hits[ip] = hits
            return False
        hits.append(now)
        _ip_hits[ip] = hits
        return True


def _send_brevo(to_email, code):
    """Возвращает None при успехе или текст ошибки."""
    payload = {
        "sender": {
            "name": os.environ.get("BREVO_SENDER_NAME", "Mijer Chat").strip() or "Mijer Chat",
            "email": os.environ["BREVO_SENDER_EMAIL"].strip(),
        },
        "to": [{"email": to_email}],
        "subject": f"Код входа Mijer Chat: {code}",
        "textContent": f"Ваш код входа в Mijer Chat: {code}\nОн действует 10 минут. "
                       f"Если вы не запрашивали код, просто проигнорируйте письмо.",
        "htmlContent": (
            "<div style=\"font-family:Arial,sans-serif;max-width:420px;margin:0 auto;padding:24px\">"
            "<h2 style=\"margin:0 0 12px\">Mijer Chat</h2>"
            "<p style=\"margin:0 0 8px\">Ваш код входа:</p>"
            f"<p style=\"font-size:32px;font-weight:700;letter-spacing:6px;margin:0 0 16px\">{code}</p>"
            "<p style=\"color:#666;font-size:13px;margin:0\">Код действует 10 минут. "
            "Если вы не запрашивали его, просто проигнорируйте это письмо.</p></div>"
        ),
    }
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": os.environ["BREVO_API_KEY"].strip(),
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        print("BREVO ERROR:", exc.code, detail)
        return "Не удалось отправить письмо. Попробуйте чуть позже"
    except Exception as exc:  # noqa: BLE001
        print("BREVO ERROR:", exc)
        return "Не удалось связаться с почтовым сервисом"


def send_code(email, ip=""):
    """Возвращает (cooldown_seconds, error, http_status)."""
    email = normalize_email(email)
    if not email:
        return None, "Введите корректную почту", 400
    if not configured():
        return None, "Вход по почте не настроен на сервере (BREVO_API_KEY, BREVO_SENDER_EMAIL)", 503
    if ip and not _ip_allowed(ip):
        return None, "Слишком много запросов. Попробуйте позже", 429

    now = int(time.time())
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM email_codes WHERE email = ?", (email,)).fetchone()
        window_start, sends = now, 0
        if row:
            wait = RESEND_COOLDOWN - (now - (row["last_sent"] or 0))
            if wait > 0:
                return None, f"Повторно отправить код можно через {wait} с", 429
            if now - (row["window_start"] or 0) < 3600:
                window_start, sends = row["window_start"], row["sends"] or 0
                if sends >= MAX_SENDS_PER_HOUR:
                    return None, "Слишком много кодов для этой почты. Попробуйте через час", 429

        code = f"{secrets.randbelow(10 ** 6):06d}"
        error = _send_brevo(email, code)
        if error:
            return None, error, 502

        conn.execute(
            "INSERT INTO email_codes (email, code_hash, expires_at, attempts, last_sent, window_start, sends) "
            "VALUES (?, ?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, "
            "expires_at = excluded.expires_at, attempts = 0, last_sent = excluded.last_sent, "
            "window_start = excluded.window_start, sends = excluded.sends, "
            "ticket_hash = NULL, ticket_expires = 0",
            (email, _hash(email, code), now + CODE_TTL, now, window_start, sends + 1),
        )
        conn.commit()
    return RESEND_COOLDOWN, None, 200


def verify_code(email, code):
    """Возвращает (ticket, error, http_status). Ticket — одноразовое подтверждение почты."""
    email = normalize_email(email)
    code = re.sub(r"\D", "", str(code or ""))
    if not email or len(code) != 6:
        return None, "Введите 6 цифр из письма", 400

    now = int(time.time())
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM email_codes WHERE email = ?", (email,)).fetchone()
        if not row or not row["code_hash"] or now > (row["expires_at"] or 0):
            return None, "Код не найден или просрочен. Запросите новый", 400
        if (row["attempts"] or 0) >= MAX_ATTEMPTS:
            return None, "Слишком много неверных попыток. Запросите новый код", 429

        if not hmac.compare_digest(row["code_hash"], _hash(email, code)):
            conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
            conn.commit()
            left = MAX_ATTEMPTS - (row["attempts"] or 0) - 1
            return None, ("Неверный код" + (f". Осталось попыток: {left}" if left > 0 else ". Запросите новый код")), 400

        ticket = secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE email_codes SET code_hash = NULL, ticket_hash = ?, ticket_expires = ? WHERE email = ?",
            (_hash(email, ticket), now + TICKET_TTL, email),
        )
        conn.commit()
    return ticket, None, 200


def check_ticket(email, ticket):
    email = normalize_email(email)
    if not email or not ticket:
        return False
    with get_connection() as conn:
        row = conn.execute("SELECT ticket_hash, ticket_expires FROM email_codes WHERE email = ?", (email,)).fetchone()
    if not row or not row["ticket_hash"] or int(time.time()) > (row["ticket_expires"] or 0):
        return False
    return hmac.compare_digest(row["ticket_hash"], _hash(email, str(ticket)))


def clear_ticket(email):
    email = normalize_email(email)
    if not email:
        return
    with get_connection() as conn:
        conn.execute("UPDATE email_codes SET ticket_hash = NULL, ticket_expires = 0 WHERE email = ?", (email,))
        conn.commit()