# ScoreOPS Inbox

Internal QA & analytics dashboard for Banco Plata customer-support conversations
(chats and calls). It ingests real support dialogues, translates them ES→EN,
auto-classifies the topic, scores the agent's answer against the official
knowledge base, and presents everything on a single-page dashboard.

> **Two product lines**, switchable from the header:
> - **PyME** — business accounts (PFAE / Persona Moral). Full pipeline: topics, quality scoring, trends, weekly KPIs, semantic dialogue grouping.
> - **Individuals** — physical-persons trial view: Real Inbox + Users, ES+EN chat, product flags, recording links (display-only, no scoring).

> 📄 A deep, step-by-step **handoff for porting into the internal perimeter** lives in [`HANDOFF.md`](HANDOFF.md) (Russian). This README is the English overview.

---

## 1. What it does (pipeline)

```
Snowflake DWH ──(pull)──▶ import ──▶ translate (ES→EN) ──▶ classify topic + score
                                                          ──▶ semantic grouping ──▶ Dashboard
Google Site (KB) ──(scraper)──▶ documents table ──▶ used as the grading reference
```

1. **Ingest** — a puller reads support dialogues from Snowflake for a date range and imports them (dedup by `task_id`).
2. **Translate** — every message is translated Spanish→English (DeepSeek) for non-Spanish reviewers.
3. **Classify + score** — one DeepSeek call per dialogue: picks a topic from a controlled 40-topic taxonomy, detects `product_line` / `direction`, and scores the agent 1–10 across 5 criteria against the knowledge base.
4. **Group** — an LLM clusters a customer's same-day chats that are really one split conversation, merges them, and re-scores the combined thread as one.
5. **Serve** — FastAPI serves the REST API and the dashboard.

## 2. Tech stack

- **Backend:** Python 3 / FastAPI / SQLAlchemy, `server.py` (one app).
- **DB:** PostgreSQL (SQLite locally).
- **Frontend:** a single file `dashboard/index.html` — vanilla JS, no build step, CSS charts.
- **LLM:** DeepSeek (`deepseek-chat`, OpenAI-compatible client) for translation, classification, scoring, grouping.
- **Auth:** Google OAuth (corporate domain) for the dashboard; an extension key for admin endpoints.
- **Hosting (pilot):** Railway (web + Postgres + a cron scraper). Temporary — see `HANDOFF.md` §10 for the internal-perimeter plan.

## 3. Repository layout

| Path | Purpose |
|---|---|
| `server.py` | The whole web app: REST API, processing, metrics, serves the dashboard |
| `db.py` | SQLAlchemy models + migrations + topic seeding |
| `topics_seed.py` | Controlled topic dictionary (40 topics, 8 categories) |
| `translate.py` | ES→EN translation (DeepSeek) |
| `evaluator.py` | Evaluator system prompt (historical; live classifier lives in `server.py`) |
| `knowledge.py` | Loads the knowledge base (from `documents`, fallback to PDFs) |
| `auth.py` | Google OAuth + extension-key auth |
| `crawl.py`, `scraper/engine.py` | KB scraper (Playwright) → `documents` table |
| `dashboard/index.html` | Entire frontend (PyME + Individuals views) |
| `scripts/pull_chats.py` | Snowflake → dashboard puller (run locally via `plata-mcp`) |
| `analysis/import_individuals.py` | One-off loader for the Individuals CSV export |
| `Procfile`, `Dockerfile.scraper` | Web start command / scraper image |

## 4. Data model (`db.py`)

- **`conversations`** — PyME support dialogues. PK `id` = `task_id`. Key fields: `type` (chat/call), `queue_name`, `customer_id`, `transcript` (JSON `[{role,text,text_en}]`), `topic`/`topic_slug`/`topic_source`(seed/llm/human)/`topic_confidence`, `product_line` (PFAE/PM/NA), `direction`, `account_type` (PFAE External/Golden, Persona Moral, No Empresa), `tariff`, `merged_into` (semantic grouping), `avg_score`, `evaluation` (JSON), `summary`, `status`, `cohort`.
- **`topics`** — controlled dictionary (`slug, name_en, name_es, category, description, status`). Seeded from `topics_seed.py`.
- **`topic_suggestions`** — emerging-topic detector proposals.
- **`documents`** — knowledge-base pages (`url, slug, title, markdown, content_hash, removed_at, internal`).
- **`individual_dialogues`** — Individuals line (separate table): `transcript` ES+EN, `record_url`, `products` (JSON flags), `tags`, `status`.
- **`crawl_runs`, `pull_runs`** — run logs for the scraper and the puller.

## 5. HTTP API (selected)

**Dashboard data (Google Bearer):**
- `GET /conversations`, `/conversations/{id}`, `/conversations/stats`, `/conversations/topic-stats`
- `GET /metrics/weekly` · `GET /topics` · `GET /customers`, `/customers/{id}`
- `POST /conversations/{id}/topic` (manual topic override → `human`)
- `GET /individuals/conversations(+/{id})`, `/individuals/customers(+/{id})`

**Admin (`X-Extension-Key` or Bearer):**
- `POST /admin/import-csv` · `POST /admin/process-conversations` (translate+topic+score, batched)
- `POST /admin/group-conversations` (semantic grouping) · `POST /admin/link-fragments`
- `POST /admin/user-accounts` (account_type/tariff enrichment)
- `POST /admin/individuals/translate` · `POST /admin/reload-knowledge`
- `POST /admin/detect-emerging-topics`, `GET /admin/topic-suggestions` (+accept/reject)

A server-side self-heal worker drains any `pending` conversations and re-groups affected days, so an interrupted pull repairs itself.

## 6. Running locally

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY=...           # required for translate/score
# DATABASE_URL defaults to sqlite:///scoreops.db
uvicorn server:app --reload
# open http://localhost:8000/dashboard/
```

`init_db()` runs on startup: creates tables, applies idempotent migrations, seeds topics.

### Environment variables

| Var | Where | Purpose |
|---|---|---|
| `DATABASE_URL` | web, scraper | Postgres connection |
| `DEEPSEEK_API_KEY` | web | LLM (translate/classify/score/group) |
| `GOOGLE_CLIENT_ID`, `ALLOWED_EMAIL_DOMAINS` | web | dashboard OAuth |
| `EXTENSION_API_KEY` | web, scraper, puller | admin-endpoint key |
| `GSITES_COOKIES`, `KB_RELOAD_URL` | scraper | KB scraping cookie + reload hook |
| `SCOREOPS_WEB_BASE`, `SCOREOPS_EXT_KEY` | puller | target service + admin key |

## 7. Ingestion (the puller)

`scripts/pull_chats.py` exports dialogues from Snowflake (via the `plata-mcp` connector, Superset `dwh`) to CSV, imports them, enriches account type/tariff from the funnel, then triggers processing + grouping.

```bash
python3 pull_chats.py --date 2026-07-12                    # one day
python3 pull_chats.py --from 2026-07-04 --to 2026-07-06    # [from, to)
```

Snowflake is internal — the puller runs on a VPN+SSO machine. In the internal perimeter it becomes a server-side job with direct Snowflake access (see `HANDOFF.md`).

## 8. Knowledge base

The scraper (`crawl.py` + `scraper/engine.py`) crawls the internal Google Site with a cookie (`GSITES_COOKIES`), writes pages to `documents`, and calls `KB_RELOAD_URL` so the web reloads the KB used for scoring. A guard prevents wiping the KB if a crawl returns suspiciously few pages (e.g. expired cookie).

## 9. Migration to the internal perimeter

The service is deliberately self-contained and configured only via env vars. Four integration points change when moving inside the company perimeter: **Snowflake access** (local connector → server job), **LLM endpoint** (external DeepSeek → approved/internal LLM), **KB source** (live scrape → bundled/internal), **auth** (Google OAuth → corporate SSO). All business logic ports as-is. Full plan and rationale: [`HANDOFF.md`](HANDOFF.md).

## 10. Notes / tech debt

- Unused legacy models `sessions/messages/calls` remain in `db.py` (empty) — safe to drop.
- The frontend is one large HTML file by design (pilot simplicity).
- No secrets are committed; `.gitignore` excludes `.env`, DBs, cookies (`clean_output/`, `_cookies.json`), and local client-data exports.
