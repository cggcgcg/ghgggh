"""Проверка Google ID-токена через официальный endpoint tokeninfo
(только стандартная библиотека, без google-auth)."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def verify_id_token(credential):
    """Возвращает (claims, error)."""
    cid = client_id()
    if not cid:
        return None, "Вход через Google не настроен на сервере (GOOGLE_CLIENT_ID)"
    if not credential or len(credential) > 4096:
        return None, "Пустой или неверный токен"

    url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": credential})
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None, "Google отклонил токен"
    except Exception:  # noqa: BLE001
        return None, "Не удалось связаться с Google, попробуйте ещё раз"

    if data.get("aud") != cid:
        return None, "Токен выдан для другого приложения"
    if data.get("iss") not in ISSUERS:
        return None, "Неверный издатель токена"
    if str(data.get("email_verified")).lower() != "true":
        return None, "Почта Google-аккаунта не подтверждена"
    try:
        if int(data.get("exp", 0)) < time.time():
            return None, "Токен просрочен"
    except (TypeError, ValueError):
        return None, "Неверный токен"
    if not data.get("sub"):
        return None, "Неверный токен"
    return data, None