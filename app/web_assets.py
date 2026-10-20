"""Веб-ресурсы Mijer Chat: установка как приложение (PWA), страницы правил.
Файл необязательный: если его нет, main.py просто не отдаёт эти страницы."""

import json
import os
import struct
import zlib

APP_NAME = "Mijer Chat"
THEME = "#3b82f6"
BG = "#0b1020"


def _contact():
    return os.environ.get("CONTACT_EMAIL", "").strip() or "(адрес почты поддержки — укажите переменную CONTACT_EMAIL)"


# ---------------- PWA ----------------
def manifest():
    return json.dumps({
        "name": APP_NAME, "short_name": "Mijer", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": BG, "theme_color": THEME,
        "description": "Мессенджер: чаты, группы, голосовые и видеозвонки",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, ensure_ascii=False)


SERVICE_WORKER = """
// Страница всегда берётся с сервера (network-first) — обновления приходят сами.
// Кэш нужен только как запасной вариант, если нет сети.
const CACHE = 'mijer-shell-v2';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return;
  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then(r => r || caches.match('/'))));
});
"""


def _png(size):
    """Иконка: синий фон и белый «пузырь» чата. Рисуем сами, без библиотек."""
    def sd_round_rect(px, py, cx, cy, hw, hh, r):
        dx, dy = abs(px - cx) - (hw - r), abs(py - cy) - (hh - r)
        ox, oy = max(dx, 0), max(dy, 0)
        return (ox * ox + oy * oy) ** 0.5 + min(max(dx, dy), 0) - r

    bg = tuple(int(THEME[i:i + 2], 16) for i in (1, 3, 5))
    rows = bytearray()
    s = float(size)
    for y in range(size):
        rows.append(0)
        for x in range(size):
            px, py = x / s, y / s
            d = sd_round_rect(px, py, 0.5, 0.46, 0.27, 0.19, 0.12)
            # хвостик пузыря: треугольник слева снизу
            tail = 0.0
            if 0.30 <= px <= 0.44 and 0.62 <= py <= 0.74:
                if (px - 0.30) <= (0.74 - py) * (0.14 / 0.12):
                    tail = 1.0
            a = max(0.0, min(1.0, 0.5 - d * s)) if d < 0.01 else 0.0
            a = max(a, tail)
            shade = 1.0 - 0.22 * (py)  # лёгкий градиент фона
            r = int(bg[0] * shade * (1 - a) + 255 * a)
            g = int(bg[1] * shade * (1 - a) + 255 * a)
            b = int(bg[2] * shade * (1 - a) + 255 * a)
            rows += bytes((r, g, b))
    raw = zlib.compress(bytes(rows), 6)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


_icon_cache = {}


def icon(size):
    if size not in (192, 512):
        size = 192
    if size not in _icon_cache:
        _icon_cache[size] = _png(size)
    return _icon_cache[size]


# ---------------- Страницы правил ----------------
_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Mijer Chat</title>
<style>body{{margin:0;background:#0b1020;color:#e7ebf5;font:16px/1.6 system-ui,sans-serif}}main{{max-width:720px;margin:0 auto;padding:28px 18px 60px}}
h1{{font-size:26px}}h2{{font-size:18px;margin-top:28px}}a{{color:#7aa7ff}}li{{margin:4px 0}}.note{{color:#9aa6c2;font-size:14px}}</style></head>
<body><main><p><a href="/">← В приложение</a></p><h1>{title}</h1>{body}<p class="note">Редакция от 18.10.2026</p></main></body></html>"""

TERMS = """
<p>Пользуясь Mijer Chat, вы соглашаетесь с этими правилами.</p>
<h2>Что нельзя</h2>
<ul><li>Спам, массовые рассылки и реклама без согласия получателей.</li>
<li>Оскорбления, травля, угрозы, разжигание ненависти.</li>
<li>Мошенничество и обман, выдача себя за другого человека.</li>
<li>Материалы, сексуализирующие несовершеннолетних, — запрещены полностью.</li>
<li>Сцены насилия, незаконный контент, нарушение чужих прав.</li></ul>
<h2>Жалобы и блокировка</h2>
<p>На любое сообщение и любого пользователя можно пожаловаться. К жалобе автоматически прикладывается выбранное сообщение и несколько предыдущих в этом чате — только они доступны модератору. За нарушение правил аккаунт может быть заблокирован.</p>
<h2>Ваш аккаунт</h2>
<p>Вы отвечаете за свой аккаунт и то, что с него отправляется. Аккаунт можно удалить в любой момент: Настройки → Профиль → «Удалить аккаунт».</p>
<h2>Возраст</h2>
<p>Сервис предназначен для пользователей, достигших возраста, с которого по закону вашей страны разрешена самостоятельная регистрация в мессенджерах.</p>
<h2>Связь</h2><p>Вопросы и обращения: {contact}</p>
"""

PRIVACY = """
<p>Эта политика объясняет, какие данные хранит Mijer Chat и что с ними происходит.</p>
<h2>Какие данные мы храним</h2>
<ul><li>Имя, username и время создания аккаунта.</li>
<li>Время последней активности (для статуса «в сети») — вы можете скрыть его в настройках.</li>
<li>Ваши сообщения, голосовые, кружки, фото и файлы, которые вы отправляете.</li>
<li>Списки контактов, блокировок, участие в группах, настройки оформления.</li>
<li>Жалобы на пользователей (вместе с приложенными сообщениями).</li></ul>
<h2>Зачем</h2><p>Только чтобы сервис работал: доставлять сообщения, показывать чаты и защищать пользователей от злоупотреблений. Мы не продаём данные и не показываем рекламу.</p>
<h2>Доступ к камере и микрофону</h2><p>Запрашивается только когда вы записываете голосовое, кружок или делаете звонок. Запись не ведётся без вашего действия.</p>
<h2>Передача третьим лицам</h2><p>Звонки идут напрямую между участниками (WebRTC); для соединения могут использоваться служебные серверы STUN/TURN. Хостинг сервиса — Railway.</p>
<h2>Хранение и удаление</h2>
<p>Данные хранятся, пока существует ваш аккаунт. При удалении аккаунта удаляются профиль, сообщения, контакты, группы, которыми вы владеете, и связанные записи. Подробнее: <a href="/delete-account">как удалить аккаунт</a>.</p>
<h2>Связь</h2><p>Вопросы о данных: {contact}</p>
"""

DELETE = """
<p>Вы можете удалить аккаунт и все связанные данные самостоятельно.</p>
<h2>Как удалить</h2>
<ol><li>Откройте Mijer Chat и войдите в аккаунт.</li><li>Настройки → Профиль → «Удалить аккаунт».</li>
<li>Введите свой username для подтверждения и нажмите «Удалить навсегда».</li></ol>
<h2>Что будет удалено</h2>
<ul><li>Профиль, настройки, список контактов и блокировок.</li><li>Все ваши сообщения и вложения.</li>
<li>Группы, которыми вы владеете (вместе с перепиской), и ваше участие в чужих группах.</li></ul>
<p>Удаление необратимо. Если нет доступа к аккаунту — напишите: {contact}</p>
"""


def page(name):
    pages = {"terms": ("Правила использования", TERMS), "privacy": ("Политика конфиденциальности", PRIVACY),
             "delete-account": ("Удаление аккаунта", DELETE)}
    if name not in pages:
        return None
    title, body = pages[name]
    return _PAGE.format(title=title, body=body.format(contact=_contact())).encode("utf-8")