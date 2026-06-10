# ScoreOPS Inbox

Сервис обработки реальных клиентских обращений Banco Plata (PFAE / cuenta PyME): Real Inbox + управляемые топики + база знаний. Выделен из основного ScoreOPS (QA-симуляции) для изоляции клиентских данных и подготовки к переезду во внутренний контур.

Полный контекст и инструкции — в [CLAUDE.md](CLAUDE.md).

- **web**: FastAPI (`server.py`) + дашборд (`dashboard/index.html`), `uvicorn server:app`.
- **БД**: Postgres (`DATABASE_URL`). Таблицы и сид топиков создаются на старте.
- **scraper**: парсер базы знаний (`Dockerfile.scraper` → `crawl.py`).
- **выгрузка чатов**: `scripts/pull_chats.py` (локально, Snowflake через plata-mcp → этот сервис).

Авторизация — корпоративный Google (`dif.tech`).
