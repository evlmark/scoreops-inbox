# ScoreOPS Inbox — контекст проекта (handoff)

> Единая точка входа для агента. Прочитай целиком перед работой.
> Общение с пользователем — **на русском**. Контент продукта (темы, база знаний, данные чатов) — **на испанском**, не переводим без явной просьбы.

## 1. Что это и зачем выделено

**ScoreOPS Inbox** — сервис обработки **реальных клиентских обращений** Banco Plata (PFAE / cuenta PyME, Мексика). Три части:
1. **Real Inbox** — реальные чаты/звонки клиентов из Snowflake, перевод на EN, авто-разметка топика и оценка качества по базе знаний (DeepSeek).
2. **Topics** — управляемый словарь топиков (таксономия v1.2), авторазметка, банк топиков, тренды, детектор новых топиков.
3. **Knowledge Base** — парсится с внутреннего Google-сайта, эталон для оценок.

Это **выделение из основного ScoreOPS** (QA-симуляции). Цель — изолировать чувствительные данные клиентов от симуляций и подготовить к **безболезненному переезду во внутренний контур компании**: сервис самодостаточен и настраивается только через env. Симуляций/Vapi здесь НЕТ.

Язык интерфейса — английский; контент — испанский.

## 2. Архитектура

```
┌─ Railway (новый проект scoreops-inbox) ─────────────────────────┐
│  web (FastAPI/uvicorn)  ← репо gitlab marik.evlampiev/scoreops-inbox, ветка main
│    server.py, dashboard/index.html  (вкладки: Real Inbox / Topics / Knowledge Base)
│  Postgres  ← вся БД сервиса
│  scraper   ← Dockerfile.scraper, cron — crawl.py → documents → reload web
└──────────────────────────────────────────────────────────────────┘
        ▲
┌─ Мак пользователя (VPN + SSO) ──────────────────────────────────┐
│  launchd pull_chats.py → plata-mcp (Superset dwh, db=1=Snowflake) → CSV
│    → POST web /admin/import-csv (дедуп по TASK_ID)
│    → POST web /admin/process-conversations (перевод+топик+оценка)
└──────────────────────────────────────────────────────────────────┘
```

**Ключевой факт:** Snowflake — внутреннее хранилище компании; из облака Railway туда не ходим, выгрузка живёт локально через `plata-mcp` (VPN+SSO). При переезде во внутренний контур пул станет серверным джобом (доступ к Snowflake изнутри), локальный Mac-хоп уйдёт.

## 3. Деплой

- **web**: автодеплой при push в `main`. Старт: `Procfile` → `uvicorn server:app`. БД — env `DATABASE_URL`. На старте `init_db()` создаёт таблицы, гонит ALTER-миграции (`db.py:_run_migrations`) и сидит топики (`seed_topics` из `topics_seed.py`).
- **scraper**: отдельный сервис, `Dockerfile.scraper`, cron. Env: `DATABASE_URL`(ref), `GSITES_COOKIES`, `KB_RELOAD_URL`, `EXTENSION_API_KEY`, `RAILWAY_DOCKERFILE_PATH=Dockerfile.scraper`.
- **Postgres**: Railway, внешний доступ — `DATABASE_PUBLIC_URL`.
- Railway PROJECT-токен не умеет создавать проекты/линковать репо — это руками в дашборде.

## 4. Авторизация (та же корп-Google, что в основном ScoreOPS)

`AuthMiddleware` в `server.py`:
- `/admin/*` — заголовок `X-Extension-Key` (дефолт в `auth.py`) ИЛИ Google Bearer.
- `/conversations*`, `/topics` и пр. — **только Google Bearer** (домены из `ALLOWED_EMAIL_DOMAINS`, дефолт `dif.tech`). Для аналитики удобнее ходить прямо в Postgres по `DATABASE_PUBLIC_URL`.

## 5. Модель данных (db.py)

- `conversations` — реальные обращения. PK `id`=`TASK_ID`. Поля: `type`(chat/call), `queue_name`, `customer_id` (плейсхолдеры `<nil>`/`null` → NULL через `clean_customer_id`), `transcript`(JSON `[{role,text,text_en}]`), `topic`/`topic_es`, `topic_slug`/`topic_source`(seed/llm/human)/`topic_confidence`, `product_line`(PFAE/PM/NA), `direction`(inbound/outbound), `avg_score`, `evaluation`, `status`, `cohort`.
- `topics` — словарь: `slug, name_en, name_es, category, description, status, sort_order`. Сид — `topics_seed.py` (40 топиков, 8 категорий, v1.2).
- `topic_suggestions` — предложения детектора новых топиков.
- `documents` — KB (страницы сайта): `url, slug, title, markdown, content_hash, removed_at, internal`(исключать из KB для оценки).
- `crawl_runs`, `pull_runs` — логи парсера KB и выгрузок чатов.
- ⚠️ Таблицы `sessions/messages/calls` остались от выделения из ScoreOPS — **не используются** (пустые). Можно удалить из db.py в рамках чистки (тогда убрать и их импорт в server.py).

## 6. Топики (управляемый словарь)

- Классификатор `classify_and_evaluate_conversation` (server.py): один вызов DeepSeek — выбирает `topic_slug` ИЗ словаря (или `proc_other`) + `product_line`/`direction`/`confidence` + оценка качества. Few-shot из подтверждённых человеком примеров (`topic_source='human'`). Авторазметка **не перетирает** ручную метку.
- `_process_one_conversation` пишет topic_slug/source/confidence/product_line/direction, резолвит topic/topic_es из имён.
- Эндпоинты: `GET /topics`, `POST /admin/topics`(+`/{id}/merge`), `POST /conversations/{id}/topic` (ручная правка, human), `GET /conversations/topic-stats` (тренды day/week/month + дельта), `POST /admin/detect-emerging-topics`, `GET /admin/topic-suggestions`(+accept/reject).
- UI: вкладка Topics — банк (CRUD/merge/archive), тренды (Topics/Groups + drill в обращения), предложения.

## 7. Источник чатов — Snowflake (через plata-mcp)

`plata-mcp` → Superset, **env=`dwh`, database_id=`1`**. Таблицы: `dwh_ops_qa_prod.customer_care.t_task_act_extra_data`, `...t_cs_bot_chats`. Фильтр PyME: `original_queue ilike '%pyme%' or pyme_account_flg=true`. SQL safety-парсер не тянет длинные запросы — бить по ~40.

## 8. Локальная выгрузка чатов (launchd)

- `scripts/pull_chats.py` (исходник) → установить в `~/.scoreops/pull_chats.py` (НЕ из ~/Downloads — TCC).
- launchd: `scripts/com.scoreops.chatpull.plist.template`, 05:00 Мехико. **Обязательно задать `SCOREOPS_WEB_BASE`** = боевой URL этого сервиса (дефолт в коде = `CHANGE-ME`).
- Запуск: `python3 ~/.scoreops/pull_chats.py [--date Y-M-D | --from A --to B | --no-process | --csv path]`. Дефолт — окно 4 дня.
- Лаг DWH >1 сут → окно 4 дня добирает дедупом. SSO Superset истекает (~раз в неделю) → `plata-mcp login`.

## 9. База знаний — парсер

- Сейчас: `crawl.py` + `scraper/engine.py` парсят Google-сайт (кука в `GSITES_COOKIES`), пишут в `documents`, дёргают `KB_RELOAD_URL`. `knowledge.py` грузит KB при старте из `documents` (fallback — PDF из `Learning Base/`).
- **План переезда во внутренний контур:** живой скрейпинг наружу с личной кукой там не разрешён. KB должна заноситься как артефакт деплоя (файлы-бандл) или из внутреннего источника/сервис-аккаунта. `knowledge.py` оставить способным грузить из файлов. Парсер с кукой → внешний инструмент подготовки бандла. Обсудить с инженерами компании.

## 10. Env (полный список для Railway)

`DATABASE_URL`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`(если нужен перевод через Gemini — см. translate.py), `GOOGLE_CLIENT_ID`, `ALLOWED_EMAIL_DOMAINS=dif.tech`, `EXTENSION_API_KEY`. Scraper: `GSITES_COOKIES`, `KB_RELOAD_URL`, `RAILWAY_DOCKERFILE_PATH=Dockerfile.scraper`. Локально pull: `SCOREOPS_WEB_BASE`, `SCOREOPS_EXT_KEY`.

## 11. Грабли

- Push в main → автодеплой. Миграции и сид топиков — авто на старте.
- `.gitignore` исключает `*.json`, `.env`, `*.db`, `logs/`. Сид топиков — `.py` (не json), иначе не задеплоится.
- Дата окружения может «прыгать» — сверяйся с `date`.
- Это сервис с данными клиентов: пока на личном Railway данные на личной инфре; финальная изоляция — переезд во внутренний контур.

## 12. История

Выделено из основного ScoreOPS (репо evlmark/ScoreOPS, там остаются симуляции Matrix/Response/Session/Phone Calls + своя копия Knowledge Base). Основной сервис не трогаем; этот работает параллельно.
