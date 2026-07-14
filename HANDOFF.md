# ScoreOPS Inbox — техническое описание для переноса во внутренний контур

> Документ для команды разработки Banco Plata. Описывает, **что это за сервис, как он
> устроен, как выглядит, какие данные и внешние зависимости использует**, и **что нужно
> поменять при переносе во внутренний контур компании**.
>
> Стек: Python 3 / FastAPI / SQLAlchemy / PostgreSQL, один HTML-файл фронта (vanilla JS),
> LLM = DeepSeek (`deepseek-chat`), OAuth = Google. Сейчас крутится на Railway (личный
> аккаунт) — это **временный хостинг для пилота**, цель — перенос внутрь.

---

## 1. Назначение

**ScoreOPS Inbox** — внутренний QA-инструмент для поддержки клиентов **PyME / PFAE / Persona
Moral** (бизнес-счета Banco Plata, Мексика). Берёт реальные обращения клиентов (чаты и
звонки) из хранилища компании, и для каждого обращения:

1. **переводит** транскрипт с испанского на английский (для не-испаноязычных ревьюеров);
2. **классифицирует тему** обращения по управляемому словарю из 40 тем (таксономия v1.2);
3. **оценивает качество ответа агента** (1–10 по 5 критериям) по официальной базе знаний;
4. показывает всё это на **дашборде**: лента обращений, тренды по темам, профили клиентов,
   недельные KPI.

Конечная ценность: видеть, **с чем приходят клиенты**, **как качественно отвечает поддержка**,
и **какие темы растут** — без ручного перечитывания тысяч чатов.

> Это **выделенный** сервис: исторически он был частью большого ScoreOPS (QA-симуляции
> с ботами), но обработку **реальных клиентских данных** вынесли в отдельный кодовый
> репозиторий именно ради переноса во внутренний контур. Симуляций/ботов/телефонии здесь
> нет — только обработка реальных обращений.

---

## 2. Как выглядит (UI)

Фронт — **один файл** `dashboard/index.html` (~1900 строк, vanilla JS, без сборки и без
фреймворков; графики на CSS, без библиотек). Раздаётся самим FastAPI по пути `/dashboard/`.
Язык интерфейса — английский, контент обращений — испанский (+ англ. перевод).

Сверху — **строка недельных KPI** с навигацией по периодам (◀/▶ недели или кастомный
диапазон дат): Chats, Users, **Avg tasks/user**, PFAE chats, PFAE users — с дельтой к
предыдущему периоду.

Ниже — **4 вкладки**:

- **📥 Real Inbox** — лента всех обращений (таблица: время, тип chat/call, тема + бейджи
  account_type/tariff/product_line/direction, агент, оценка, статус). Сверху — KPI дня и
  компактный график **«Conversations per day»** (столбики chats+calls, с тултипом по
  наведению). Клик по строке → модалка обращения с полным транскриптом (оригинал + перевод
  построчно), оценкой по 5 критериям, AI-резюме, редактором темы, историей контактов этого
  же клиента и ссылкой на его профиль.
- **🏷 Topics** — управление словарём тем (CRUD/merge/archive), **тренды** по темам и
  категориям (текущее окно vs предыдущее + дельта; пресеты день/неделя/месяц или кастомный
  диапазон), сегментный фильтр **All / None Empresa / PFAE / PM**, drill-down в обращения по
  теме. По каждой теме показываются **2 метрики: число чатов и число уникальных клиентов**.
  Плюс блок «emerging topics» — предложения новых тем от детектора.
- **👥 Users** — список клиентов (по `customer_id`) и **профиль клиента**: вся история его
  обращений по датам, инлайн-транскрипты, account_type/tariff, агрегаты (кол-во, темы, ср.
  оценка). Кросс-ссылки чат ↔ профиль.
- **🧠 Knowledge Base** — просмотр статей базы знаний (эталон для оценки качества).

---

## 3. Архитектура и потоки данных

```
                ВНЕШНИЕ ЗАВИСИМОСТИ
   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
   │  Snowflake  │   │   DeepSeek   │   │  Google OAuth    │
   │ (DWH компа- │   │  LLM API     │   │ (вход в дашборд) │
   │   нии)      │   │ перевод+оцен.│   └──────────────────┘
   └──────┬──────┘   └──────┬───────┘
          │ (1) выгрузка    │ (3) перевод/классиф./оценка
          ▼                 ▼
   ┌──────────────────────────────────────────────────────┐
   │  WEB-сервис (FastAPI / uvicorn)  — server.py          │
   │  • REST API (≈30 эндпоинтов)                          │
   │  • раздаёт дашборд (dashboard/index.html)             │
   │  • вся бизнес-логика: импорт, обработка, метрики      │
   └───────────┬──────────────────────────────────────────┘
               │ ORM (SQLAlchemy)
               ▼
   ┌──────────────────────┐      ┌─────────────────────────┐
   │   PostgreSQL          │◄────│  SCRAPER (cron-джоб)     │
   │  conversations,       │     │  crawl.py + scraper/    │
   │  topics, documents,   │     │  парсит базу знаний →   │
   │  pull_runs, ...       │     │  таблица documents      │
   └──────────────────────┘      └─────────────────────────┘
               ▲
               │ (2) POST /admin/import-csv + /admin/process-conversations
   ┌───────────┴──────────────────────────────────────────┐
   │  PULLER  — scripts/pull_chats.py                       │
   │  выгружает обращения из Snowflake → CSV → шлёт в web    │
   │  СЕЙЧАС: запускается локально (VPN+SSO) по cron/вручну │
   └───────────────────────────────────────────────────────┘
```

### End-to-end поток одного обращения

1. **Выгрузка (ingestion).** `pull_chats.py` ходит в Snowflake (DWH), берёт обращения за
   период, склеивает реплики в транскрипт, выгружает в CSV, и шлёт в web:
   `POST /admin/import-csv` (дедуп по `task_id`). Затем тянет из funnel тип счёта и тариф
   клиентов → `POST /admin/user-accounts`.
2. **Обработка (processing).** Пуллер вызывает `POST /admin/process-conversations` (батчами).
   Для каждого нового обращения web:
   - переводит каждую реплику EN (`translate.py` → DeepSeek);
   - **одним вызовом DeepSeek** определяет тему + product_line + direction + confidence и
     оценивает агента по базе знаний (`classify_and_evaluate_conversation`);
   - пишет результат, ставит `status='done'`.
3. **Склейка фрагментов.** `POST /admin/link-fragments` помечает короткие чаты-«продолжения»
   (когда поддержка долго не отвечала и диалог разбился на несколько чатов) как `merged_into`
   основного — чтобы не считать и не оценивать их отдельно.
4. **Отображение.** Дашборд читает данные через REST (`/conversations`, `/topics`,
   `/metrics/weekly`, ...) и рисует ленту/тренды/профили.

> Шаги 1–3 сейчас инициирует **пуллер** (внешний по отношению к web). Логика 2–3 целиком
> живёт в web. Для внутреннего контура пуллер станет серверным джобом (см. §10).

---

## 4. Модель данных (PostgreSQL, `db.py`)

Создаётся автоматически при старте: `init_db()` → `create_all` + идемпотентные ALTER-миграции
(`_run_migrations`, отдельные ветки для SQLite/Postgres) + сид словаря тем (`seed_topics`).

**`conversations`** — главная таблица, одно обращение = одна строка. PK `id` = `task_id` из
Snowflake.

| Поле | Тип | Описание |
|---|---|---|
| `id` | str (PK) | = `task_id` |
| `type` | str | `chat` / `call` |
| `queue_name` | str | очередь обращения (Inbound PyME Chats и т.д.) |
| `customer_id` | str | id клиента; плейсхолдеры `<nil>`/`null` → NULL (`clean_customer_id`) |
| `agent_name` | str | имя агента |
| `transcript` | JSON | `[{role:'customer'/'agent', text, text_en}]` |
| `topic`, `topic_es` | str | имя темы (EN/ES) — резолвится из словаря |
| `topic_slug` | str | slug темы из словаря `topics` |
| `topic_source` | str | `seed` / `llm` / `human` (источник метки) |
| `topic_confidence` | float | уверенность классификатора 0..1 |
| `product_line` | str | `PFAE` / `PM` / `NA` |
| `direction` | str | `inbound` / `outbound` |
| `account_type` | str | `PFAE External` / `PFAE Golden` / `Persona Moral` / `No Empresa account` (из funnel) |
| `tariff` | str | имя тарифа открытого счёта (из funnel) |
| `merged_into` | str | id основного диалога, если это короткий фрагмент-продолжение |
| `avg_score` | float | итоговая оценка 1..10 |
| `evaluation` | JSON | разбивка оценки (5 критериев, explanation, critical_error, resolved) |
| `summary` | str | AI-резюме |
| `status` | str | `pending` / `done` / `failed` |
| `cohort` | str | метка выборки (для разовых аналитических когорт; обычный inbox = NULL) |
| `created_at`, `in_progress_at`, `closed_at`, `imported_at` | datetime | таймстемпы |

**`topics`** — управляемый словарь тем: `slug, name_en, name_es, category, description,
status(active/archived), sort_order`. Сид — `topics_seed.py`: **40 тем, 8 категорий**
(Account opening 5, Product information 5, Application status 4, Access/security 5, Card 5,
Account operations 9, Routing/non-PyME 2, Process/no-intent 5).

**`topic_suggestions`** — предложения новых тем от детектора (`proposed_name, category,
rationale, count, sample_conv_ids, status`).

**`documents`** — база знаний: `url, slug, title, markdown, content_hash, first_seen,
last_seen, removed_at, internal`. `internal=1` — агентские инструкции, исключаются из KB
при оценке.

**`crawl_runs`**, **`pull_runs`** — логи запусков скрапера KB и выгрузок чатов.

> ⚠️ Технический долг: в `db.py` остались неиспользуемые модели `sessions/messages/calls`
> (пустые, наследие от большого ScoreOPS) — можно удалить при чистке.

---

## 5. LLM-обработка (ядро ценности)

Всё через **DeepSeek** (OpenAI-совместимый клиент, `base_url=https://api.deepseek.com`,
модель `deepseek-chat`). Два места:

**Перевод** (`translate.py`): по реплике, ES→EN, system-prompt «верни только перевод».

**Классификация + оценка** (`classify_and_evaluate_conversation` в `server.py`) — **один
вызов** на обращение, в промпт подаётся:
- вся **база знаний** (текст всех активных `documents`);
- **каталог тем** (slug | name | description активных тем);
- **few-shot**: примеры тем, подтверждённых человеком (`topic_source='human'`) — так ручные
  правки автоматически дообучают классификатор без файнтюна;
- сам транскрипт.

Возвращает JSON: `topic_slug` (валидируется против словаря, иначе `proc_other`),
`product_line`, `direction`, `topic_confidence`, `score`, `breakdown` (precision, language,
protocol, completeness, empathy), `explanation`, `critical_error`, `resolved`.

**Принцип:** список тем определяет человек, LLM только **выбирает из словаря** и **никогда не
перетирает ручную метку** (`topic_source='human'`).

**Детектор новых тем** (`/admin/detect-emerging-topics`): кластеризует обращения с
`proc_other`/низкой уверенностью через DeepSeek и предлагает новые темы (не создаёт сам).

**Склейка фрагментов** (`_link_fragments`): чисто алгоритмическая (без LLM). Обращения
одного клиента за один день, идущие подряд с разрывом ≤10 мин, образуют кластер; «основной» =
с макс. числом реплик; остальные с ≤2 репликами клиента → `merged_into` основного.
Полный идемпотентный пересчёт.

---

## 6. REST API (≈30 эндпоинтов, `server.py`)

**Служебные:** `GET /health`, `GET /auth-config`, `GET /privacy`.

**Обращения / метрики (Google Bearer):**
- `GET /conversations` — лента (фильтры: cohort, topic_slug, days, from_date/to_date,
  customer_id, exclude_outbound, include_merged).
- `GET /conversations/{id}` — полные данные обращения + история клиента + дочерние фрагменты.
- `GET /conversations/stats` — статистика по дням + топ тем.
- `GET /conversations/topic-stats` — тренды по темам (окно vs предыдущее, чаты+уникальные
  юзеры, сегмент all/none/pfae/pm, кастомный диапазон).
- `POST /conversations/{id}/topic` — ручная правка темы (ставит `topic_source='human'`).
- `GET /metrics/weekly` — недельные KPI (сдвиг недель / кастомный период; avg tasks/user).
- `GET /topics` — словарь тем со счётчиками.
- `GET /customers`, `GET /customers/{id}` — список и профиль клиента.

**Админские (`X-Extension-Key` ИЛИ Google Bearer):**
- `POST /admin/import-csv` — импорт обращений (дедуп по task_id).
- `POST /admin/process-conversations?batch=N` — обработка `pending` батчами
  (перевод+тема+оценка); возвращает `{processed_this_batch, remaining_pending, done}`.
- `POST /admin/link-fragments` — пересчёт склейки фрагментов.
- `POST /admin/user-accounts` — bulk-апдейт account_type/tariff по customer_id (из funnel).
- `POST /admin/topics`, `POST /admin/topics/{id}/merge` — CRUD/merge словаря.
- `POST /admin/detect-emerging-topics`, `GET /admin/topic-suggestions`,
  `POST /admin/topic-suggestions/{id}/accept|reject` — детектор новых тем.
- `POST /admin/reload-knowledge`, `GET /admin/kb-documents`, `GET /admin/crawl-runs` — KB.
- `GET/POST /admin/pull-runs` — лог выгрузок.

---

## 7. Источники данных (то, что лежит ВНУТРИ компании)

**Обращения — Snowflake DWH** (`scripts/pull_chats.py`, SQL в `build_chats_sql`):
- `dwh_ops_qa_prod.customer_care.t_task_act_extra_data` — карточки обращений (task_id,
  queue_name, customerid, таймстемпы, `original_queue`, `pyme_account_flg`).
- `dwh_ops_qa_prod.customer_care.t_cs_bot_chats` — тексты реплик (склеиваются `LISTAGG`).
- Фильтр PyME сейчас: `original_queue ilike '%pyme%' OR pyme_account_flg = true`.
  ⚠️ Это **шире** корп-дашборда (который берёт строго `queue_name IN ('Inbound PyME Chats',
  'Inbound PyME Calls')`) — даёт больше обращений (ловит PyME-клиентов в общих очередях).
  При интеграции согласовать единое определение.

**Тип счёта и тариф — Snowflake funnel:**
- `DWH_PYME_MAIN_PROD.ORIGINATION.FUNNEL_PFAE` — join по `user_id` = наш `customer_id`;
  `current_status='ACCOUNT_CREATED'`, `product_type` (PFAE/PFAEGolden), `tariff_name`.
- `DWH_PYME_MAIN_PROD.ORIGINATION.FUNNEL_PM` (Persona Moral) — нет `user_id`; матчим наш
  `customer_id` по `legal_representative_identity_id` ИЛИ `stakeholder_id`.

**База знаний** — сейчас парсится скрапером с внутреннего Google-сайта (`crawl.py` +
`scraper/engine.py`, кука в env `GSITES_COOKIES`) в таблицу `documents`. Фолбэк — PDF из
папки `Learning Base/` (`knowledge.py`).

---

## 8. Аутентификация (`auth.py`)

`AuthMiddleware` в `server.py`:
- `/admin/*` — заголовок `X-Extension-Key` **или** Google Bearer-токен.
- `/conversations*`, `/topics`, `/customers*`, `/metrics*` — **только Google Bearer**
  (проверяется через Google tokeninfo; домены из `ALLOWED_EMAIL_DOMAINS`). Время жизни
  токена ~1 час (Google ID token).
- статика дашборда и `/health`, `/auth-config`, `/privacy` — открыты.

---

## 9. Текущая инфраструктура (Railway, временная)

- **web**: `Procfile` → `uvicorn server:app`. Автодеплой при push в `main`. На старте —
  миграции + сид тем.
- **PostgreSQL**: managed Postgres.
- **scraper**: отдельный сервис, `Dockerfile.scraper` (Playwright/Chromium), запуск по cron.
- **puller**: `scripts/pull_chats.py` — **сейчас локально** на машине сотрудника (VPN+SSO к
  Snowflake), launchd-таймер раз в сутки.

**Зависимости** (`requirements.txt`): fastapi, uvicorn, sqlalchemy, psycopg2-binary, openai
(для DeepSeek), pypdf, httpx, python-dotenv, pydantic, aiofiles.

**Переменные окружения:**
- web: `DATABASE_URL`, `DEEPSEEK_API_KEY`, `GOOGLE_CLIENT_ID`, `ALLOWED_EMAIL_DOMAINS`,
  `EXTENSION_API_KEY`.
- scraper: `DATABASE_URL`, `GSITES_COOKIES`, `KB_RELOAD_URL`, `EXTENSION_API_KEY`.
- puller: `SCOREOPS_WEB_BASE`, `SCOREOPS_EXT_KEY` (+ доступ к Snowflake через локальный
  коннектор).

---

## 10. План переноса во внутренний контур (что менять разработчикам)

Сервис **сознательно спроектирован самодостаточным** (всё через env, без жёстких внешних
привязок). Ключевые точки интеграции:

1. **Пуллер → серверный джоб.** Сейчас выгрузка идёт локально через внешний коннектор к
   Snowflake (VPN+SSO) и шлёт данные по HTTP. Внутри контура — переписать на **прямой
   доступ к Snowflake** изнутри (сервис-аккаунт), оформить как cron-джоб/airflow-DAG рядом с
   web. SQL уже готов (`build_chats_sql`), HTTP-хоп (`/admin/import-csv` +
   `/admin/process-conversations` + `/admin/link-fragments`) можно сохранить или заменить
   прямыми вызовами функций. **Важно:** после импорта обязательно дожимать `pending`
   (прерванная обработка оставляет необработанные обращения).
2. **LLM.** Перевод и классификация/оценка идут через **внешний DeepSeek API**. Внутри
   контура — либо разрешить исходящий доступ к выбранному LLM-провайдеру, либо подключить
   **внутреннюю LLM-площадку** (клиент OpenAI-совместимый, меняется только `base_url`/`api_key`
   и имя модели в `translate.py` и `server.py`). Объём: ~1 LLM-вызов на перевод реплики +
   1 вызов на классификацию/оценку обращения.
3. **База знаний.** Живой скрейпинг внешнего Google-сайта с личной кукой внутри контура не
   нужен/не разрешён. KB заносить как **артефакт деплоя** (бандл файлов) или из внутреннего
   источника/сервис-аккаунта; `knowledge.py` уже умеет грузить из файлов (фолбэк) и из
   таблицы `documents`. Скрапер можно вынести во внешний инструмент подготовки бандла.
4. **OAuth.** Сейчас Google OAuth по корп-домену. Внутри — заменить на **корпоративный SSO**
   (Keycloak/SAML/OIDC). Точка изоляции — `auth.py` (`verify_*` + middleware), фронт ждёт
   Bearer-токен.
5. **БД.** Любой PostgreSQL; схема создаётся сама. Перенос — стандартный dump/restore.
6. **Секреты.** `EXTENSION_API_KEY`, ключ LLM, строка БД — в Vault/секрет-менеджер контура.
7. **Данные клиентов.** Это PII (транскрипты, customer_id). Внутри контура — соблюсти
   требования по хранению/доступу; дашборд закрыт SSO, БД — приватная.

**Что НЕ требует изменений:** вся бизнес-логика (обработка, темы, метрики, склейка, UI) —
переносится как есть; меняются только 4 точки интеграции (Snowflake-доступ, LLM-endpoint,
источник KB, SSO).

---

## 11. Структура репозитория

```
server.py            — весь web: API, обработка, метрики, раздача дашборда
db.py                — модели SQLAlchemy + миграции + сид тем
topics_seed.py       — словарь из 40 тем (сид)
translate.py         — перевод ES→EN (DeepSeek)
evaluator.py         — system-prompt оценщика (использовался ранее; актуальный
                       классификатор+оценка живёт в server.py)
knowledge.py         — загрузка базы знаний (из documents, фолбэк PDF)
auth.py              — Google OAuth + ключ расширения
crawl.py             — точка входа скрапера KB
scraper/engine.py    — парсинг Google-сайта (Playwright)
kb_import.py / import_knowledge.py — импорт KB
dashboard/index.html — весь фронт (vanilla JS)
scripts/pull_chats.py — выгрузка из Snowflake (пуллер)
scripts/com.scoreops.chatpull.plist.template — launchd-таймер (локально)
Procfile             — запуск web
Dockerfile.scraper   — образ скрапера
requirements*.txt    — зависимости
```

---

## 12. Известные ограничения / технический долг

- Фильтр выборки PyME шире корп-определения (см. §7) — согласовать.
- Прерванная обработка оставляет `pending`-обращения без перевода/темы/оценки — нужно
  гарантированно дожимать `pending` при следующем запуске (рекомендуется встроить в джоб).
- Неиспользуемые модели `sessions/messages/calls` в `db.py`.
- Фронт — один большой HTML-файл без сборки (осознанно, ради простоты пилота).
- Зависимость от внешнего DeepSeek и Google OAuth — обе заменяемы (см. §10).
