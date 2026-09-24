# bakermusic

Telegram-бот: поиск и скачивание музыки из YouTube и SoundCloud.

## Переменные окружения

| Переменная | Обязательна | Что это |
|---|---|---|
| `TELEGRAM_TOKEN` | да | токен бота от @BotFather |
| `PROXY_URL` | нет | локальный прокси VPN-клиента. Для Happ: `socks5://127.0.0.1:10808` |
| `SOURCES` | нет | включённые источники и порядок кнопок, по умолчанию `yt,sc` |
| `YTDLP_COOKIES` | нет | путь к cookies.txt YouTube (если YouTube просит «confirm you're not a bot») |
| `START_GIF` | нет | file_id / URL гифки. По умолчанию берётся `assets/start.gif` |
| `WEBHOOK_URL`, `WEBHOOK_SECRET`, `PORT` | нет | режим вебхука; без них бот работает через polling |

## GIF на главном экране

Положите гифку в `assets/start.gif` (лучше 1–3 МБ).
После первой отправки бот запоминает её file_id и не загружает повторно.

## Запуск локально (Windows, PowerShell)

```powershell
winget install Gyan.FFmpeg DenoLand.Deno
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # и впишите в .env свой токен
python music.py
```

Все переменные удобно хранить в файле `.env` рядом с `music.py` — бот читает его сам при запуске.

Треки SoundCloud Go+ доступны только как 30-секундное превью — бот их не отправляет и сообщает об этом.
Цветные кнопки видны в клиентах Telegram, выпущенных после февраля 2026.
