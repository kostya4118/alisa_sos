# Перенос Alisa SOS на новый сервер

Весь сервис — это **три вещи**: код (из git), файл `.env` (секреты) и папка
`data/` (вся база + сессия MAX). Домен остаётся тот же, поэтому **владельцам
ничего переделывать не нужно** — URL Алисы и ссылки-приглашения не меняются.

## Что переносим

| Что | Где | Зачем |
|-----|-----|-------|
| Код | git (нужная ветка) | приложение |
| `.env` | корень проекта | все токены и настройки |
| `data/` | папка рядом с проектом | БД (`sos.db`), сессия MAX (`data/max_session/`), бэкапы |

Всё состояние — в `data/`. Скопировал её + `.env` → перенёс всё: владельцев,
контакты, ответы, вход в MAX.

> Ниже в примерах домен `kostyalisos.data-annotation.ru` и внутренний порт
> приложения `44118` (значение `PORT` из `.env`). Подставьте свои.

---

## Шаг 1. На СТАРОМ сервере — снять консистентную копию

Останавливаем бот (чтобы SQLite дописал WAL), архивируем `data/` и `.env`:

```bash
cd ~/alisa_sos
docker compose down
tar czf ~/alisa_migrate.tar.gz data .env
```

Скачиваем архив на локальную машину:

```bash
scp root@СТАРЫЙ_IP:~/alisa_migrate.tar.gz .
```

## Шаг 2. На НОВОМ сервере — Docker + код

```bash
# Docker (Ubuntu)
curl -fsSL https://get.docker.com | sh

# Код
cd ~
git clone https://github.com/kostya4118/alisa_sos.git
cd alisa_sos
git checkout <нужная-ветка>
```

## Шаг 3. Залить состояние

```bash
scp alisa_migrate.tar.gz root@НОВЫЙ_IP:~/alisa_sos/
# на новом сервере:
cd ~/alisa_sos
tar xzf alisa_migrate.tar.gz     # появятся data/ и .env
rm alisa_migrate.tar.gz
```

## Шаг 4. Поднять

```bash
docker compose up -d --build
docker compose logs --tail=30 bot
```

В логах ждём, что Telegram-бот запустился, и (если включён MAX-userbot)
`MAX userbot ready` — сессия перенеслась, **SMS не понадобится**.

## Шаг 5. Nginx + HTTPS

```bash
sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx
```

Конфиг (порт — из вашего `.env`):

```bash
sudo tee /etc/nginx/sites-available/kostyalisos > /dev/null <<'EOF'
server {
    listen 80;
    server_name kostyalisos.data-annotation.ru;
    location / {
        proxy_pass http://127.0.0.1:44118;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/kostyalisos /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Шаг 6. Переключить DNS

В панели домена сменить **A-запись** на **IP нового сервера**. Дождаться
обновления (обычно минуты–час):

```bash
dig +short kostyalisos.data-annotation.ru   # должен показать новый IP
```

## Шаг 7. Выпустить сертификат (когда DNS уже указывает на новый сервер)

```bash
sudo certbot --nginx -d kostyalisos.data-annotation.ru
```

Certbot сам добавит 443-блок и редирект. После этого верните правки
безопасности в 443-блок (см. `SECURITY` ниже).

## Шаг 8. Проверить и погасить старый

```bash
curl -sI https://kostyalisos.data-annotation.ru/health   # 200
```

Отправьте тестовый SOS из бота. Убедившись, что новый сервер работает, на
старом выполните `docker compose down` (не удаляйте сразу — пусть постоит
день как резерв).

---

## Правки безопасности (вернуть после certbot)

В `server`-блок с `listen 443` добавить:

```nginx
    server_tokens off;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;

    location ~ ^/(openapi\.json|docs|redoc)$ { return 404; }
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Важные нюансы

- **Домен тот же** → `BASE_URL` в `.env` не меняется, URL Алисы и ссылки-
  приглашения прежние, никто ничего не переделывает.
- **Даунтайм** — только на время DNS-переключения (шаг 6). Без простоя:
  поднимите новый сервер полностью (шаги 2–5) заранее, проверьте через IP,
  и только потом переключайте DNS.
- **Сессия MAX** переносится в `data/max_session/` — повторный вход по SMS
  не нужен. Если MAX насторожится из-за нового IP и разлогинит — войдёте
  заново по `/maxcode` (userbot сам попросит).
- **Секреты** (`.env`) передавайте безопасно (scp), не через публичные каналы.
- **Файрвол:** откройте порты 80/443 (`ufw allow 80,443/tcp`).
- **Альтернатива копированию `data/`:** можно снять бэкап командой `/backup`
  в боте, а на новом сервере восстановить его через `/restore`. Но простое
  копирование `data/` переносит вообще всё, включая сессию MAX.

Итого: ~20–30 минут работы плюс ожидание DNS.
