import os
import re
import json
import uuid
import time
import threading
import warnings
from datetime import datetime, timedelta
from typing import Optional
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy import func, desc
from sqlalchemy.orm.attributes import flag_modified

from db import (
    init_db, SessionLocal,
    Session as DBSession, Message as DBMessage, Call as DBCall,
    Conversation as DBConversation, detect_agent_name,
    Document as DBDocument, CrawlRun as DBCrawlRun, PullRun as DBPullRun,
    Topic as DBTopic, TopicSuggestion as DBTopicSuggestion,
    IndividualDialogue as DBIndividual,
)

warnings.filterwarnings("ignore")

load_dotenv()

from knowledge import load_knowledge_base
from evaluator import Evaluator, client as EVALUATOR_CLIENT
from translate import translate_to_english

# Авторизация (лениво, не падаем если импорт сломан)
try:
    from auth import verify_google_token, EXTENSION_API_KEY, ALLOWED_DOMAINS, GOOGLE_CLIENT_ID
    AUTH_AVAILABLE = True
except Exception as _auth_err:
    print(f"[auth] IMPORT FAILED: {_auth_err} — auth middleware will be no-op!")
    AUTH_AVAILABLE = False
    GOOGLE_CLIENT_ID = ""
    ALLOWED_DOMAINS = set()
    EXTENSION_API_KEY = ""
    def verify_google_token(t): raise ValueError("auth module unavailable")

# Инициализируем БД (создаём таблицы если их нет)
init_db()

app = FastAPI(title="ScoreOPS API")

# CORS — разрешаем запросы от Chrome Extension и дашборда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth middleware: Google OAuth (tokeninfo) + extension API key ────────────
PUBLIC_PREFIXES = ("/dashboard",)
PUBLIC_EXACT    = {"/", "/health", "/privacy", "/openapi.json", "/docs", "/redoc", "/auth-config"}

def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    for p in PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    return False

def _is_extension_path(path: str) -> bool:
    if path.startswith("/admin/"):    # админ-эндпоинты тоже принимают X-Extension-Key
        return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        if _is_public(path):
            return await call_next(request)

        # Если авторизация не настроена (импорт упал) — пропускаем всё, не блокируем сервис
        if not AUTH_AVAILABLE:
            return await call_next(request)

        # Chrome-расширение
        if _is_extension_path(path):
            if request.headers.get("X-Extension-Key") == EXTENSION_API_KEY:
                return await call_next(request)

        # Bearer JWT (Google)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                info = verify_google_token(auth_header[7:])
                request.state.user = info
                return await call_next(request)
            except Exception as e:
                return JSONResponse(status_code=401, content={"error": f"Auth failed: {str(e)[:200]}"})

        return JSONResponse(status_code=401, content={"error": "Authentication required"})


app.add_middleware(AuthMiddleware)


@app.get("/auth-config")
def auth_config():
    return {
        "google_client_id": GOOGLE_CLIENT_ID,
        "allowed_domains":  sorted(ALLOWED_DOMAINS),
        "auth_enabled":     AUTH_AVAILABLE,
    }

# Dashboard static files
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")
if os.path.isdir(DASHBOARD_DIR):
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")

# ── Загружаем базу знаний один раз при старте ───────────────────────────────
print("Загружаем базу знаний...")
KNOWLEDGE_BASE = load_knowledge_base()
EVALUATOR = Evaluator(KNOWLEDGE_BASE)
print("Готово. Сервер запущен на http://localhost:8000\n")


# ══════════════════════════════════════════════════════════════════════════════
#  ИМПОРТ РЕАЛЬНЫХ ОБРАЩЕНИЙ (CSV → conversations)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_dttm(s: str):
    """Парсит дату-время в разных форматах → datetime. None если не получилось."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",   # 2026-05-20 19:48:29.682
        "%Y-%m-%d %H:%M:%S",      # 2026-05-20 1:40:34
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_chat_text(raw: str):
    """CHAT_TEXT 'client | текст; agent | текст; ...' → [{role, text}].
    role нормализуется: client→customer, agent→agent."""
    turns = []
    if not raw or not raw.strip():
        return turns
    # Разбиваем по '; ' но аккуратно — реплики разделены '; ' с префиксом роли
    import re as _re
    # Находим границы по шаблону "(; )?(client|agent) | "
    parts = _re.split(r";\s*(?=(?:client|agent)\s*\|)", raw)
    for p in parts:
        p = p.strip()
        if "|" not in p:
            continue
        role_raw, _, text = p.partition("|")
        role_raw = role_raw.strip().lower()
        text = text.strip()
        if not text:
            continue
        role = "customer" if role_raw == "client" else "agent"
        turns.append({"role": role, "text": text})
    return turns


_BAD_CUSTOMER_IDS = {"", "<nil>", "nil", "null", "none", "n/a", "na", "-"}


def clean_customer_id(v):
    """Плейсхолдеры (<nil>, null, пусто) → None, чтобы не склеивать чаты «без клиента»."""
    v = (v or "").strip()
    return None if v.lower() in _BAD_CUSTOMER_IDS else v


# ── Управляемый словарь топиков (таксономия v1.2) ───────────────────────────
_TOPICS_CACHE = {"data": None}


def load_topics(force: bool = False) -> list:
    """Активные топики из БД (кэш). [{slug, name_en, name_es, category, description}]."""
    if _TOPICS_CACHE["data"] is not None and not force:
        return _TOPICS_CACHE["data"]
    db = SessionLocal()
    try:
        rows = db.query(DBTopic).filter_by(status="active").order_by(DBTopic.sort_order).all()
        data = [{
            "slug": t.slug, "name_en": t.name_en, "name_es": t.name_es,
            "category": t.category, "description": t.description,
        } for t in rows]
    finally:
        db.close()
    _TOPICS_CACHE["data"] = data
    return data


def invalidate_topics_cache():
    _TOPICS_CACHE["data"] = None


def topic_names(slug: str):
    """(name_en, name_es) по slug — для обратной совместимости полей topic/topic_es."""
    for t in load_topics():
        if t["slug"] == slug:
            return t["name_en"], t["name_es"]
    return None, None


def _topic_fewshot(max_per_topic: int = 2, max_total: int = 40) -> str:
    """Подтверждённые человеком примеры (topic_source='human') как few-shot-якоря.
    Берём первую реплику клиента из транскрипта — короткий пример границы топика."""
    db = SessionLocal()
    try:
        rows = (db.query(DBConversation)
                  .filter(DBConversation.topic_source == "human",
                          DBConversation.topic_slug.isnot(None))
                  .order_by(desc(DBConversation.imported_at))
                  .limit(400).all())
    except Exception:
        rows = []
    finally:
        db.close()
    per = {}
    out = []
    for c in rows:
        if len(out) >= max_total:
            break
        if per.get(c.topic_slug, 0) >= max_per_topic:
            continue
        first_cust = next((t.get("text") for t in (c.transcript or [])
                           if t.get("role") == "customer" and t.get("text")), None)
        if not first_cust:
            continue
        per[c.topic_slug] = per.get(c.topic_slug, 0) + 1
        out.append(f'- {c.topic_slug}: "{first_cust[:120]}"')
    return "\n".join(out)


def classify_and_evaluate_conversation(transcript: list) -> dict:
    """Один вызов DeepSeek: топик ИЗ СЛОВАРЯ + атрибуты + оценка по базе знаний.
    transcript — [{role:'customer'/'agent', text}]."""
    if not transcript:
        return {}
    lines = []
    for t in transcript:
        if not t.get("text"):
            continue
        speaker = "Cliente" if t["role"] == "customer" else "Agente de soporte"
        lines.append(f"{speaker}: {t['text']}")
    convo = "\n".join(lines)

    topics = load_topics()
    catalog = "\n".join(f"- {t['slug']} | {t['name_en']} | {t['description']}" for t in topics)
    fewshot = _topic_fewshot()
    fewshot_block = f"\nEJEMPLOS CONFIRMADOS (slug: primera frase del cliente):\n{fewshot}\n" if fewshot else ""

    prompt = f"""Eres un evaluador de calidad de soporte de Banco Plata (cuentas de negocio PFAE y Persona Moral/Empresa).

BASE DE CONOCIMIENTOS OFICIAL:
{KNOWLEDGE_BASE}

CATÁLOGO DE TEMAS (elige EXACTAMENTE UNO por su slug; si nada encaja usa "proc_other"):
{catalog}
{fewshot_block}
CONVERSACIÓN REAL ENTRE CLIENTE Y AGENTE:
{convo}

Tareas:
1. topic_slug: el TEMA principal del contacto del CLIENTE — un slug del catálogo (o "proc_other").
2. product_line: "PFAE", "PM" o "NA" si no se distingue.
3. direction: "outbound" si el contacto lo inició el banco (campaña, "intenté llamarte", reposición, push); si no "inbound".
4. topic_confidence: 0.0-1.0.
5. Evalúa al agente (1-10 cada uno): precision (info correcta según la base), language (español correcto), protocol (flujo correcto), completeness (resolvió), empathy (profesional).

Responde SOLO con JSON válido, sin markdown:
{{"topic_slug": "<slug>", "product_line": "<PFAE|PM|NA>", "direction": "<inbound|outbound>", "topic_confidence": <0-1>, "score": <1-10>, "breakdown": {{"precision": <1-10>, "language": <1-10>, "protocol": <1-10>, "completeness": <1-10>, "empathy": <1-10>}}, "explanation": "<2-3 sentences in English>", "critical_error": <true|false>, "resolved": <true|false>}}

critical_error = true solo si el agente dio información factualmente incorrecta sobre productos PFAE/PyME."""

    try:
        resp = EVALUATOR_CLIENT.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```json\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        result = json.loads(content)
        # Валидация slug: неизвестный → proc_other
        valid = {t["slug"] for t in topics}
        if result.get("topic_slug") not in valid:
            result["topic_slug"] = "proc_other"
        return result
    except Exception as e:
        print(f"[classify_eval] error: {e}")
        return {}

# --- Эндпоинты ---

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/translate-test")
def translate_test(text: str = "Hola, ¿cómo estás?"):
    """Синхронный тест перевода — возвращает результат прямо в ответе."""
    import traceback
    has_key = bool(os.getenv("DEEPSEEK_API_KEY"))
    try:
        result = translate_to_english(text)
        return {
            "has_deepseek_key": has_key,
            "input": text,
            "output": result,
            "success": result is not None,
        }
    except Exception as e:
        return {
            "has_deepseek_key": has_key,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  РЕАЛЬНЫЕ ОБРАЩЕНИЯ — импорт, обработка, выдача
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/customer-stats")
def customer_stats(min_tasks: int = 2):
    """Показывает клиентов с несколькими обращениями (>=min_tasks)."""
    db = SessionLocal()
    try:
        rows = db.query(DBConversation).filter(DBConversation.customer_id.isnot(None)).all()
        from collections import defaultdict
        by_cust = defaultdict(list)
        for r in rows:
            by_cust[r.customer_id].append(r)
        multi = [(cid, convs) for cid, convs in by_cust.items() if len(convs) >= min_tasks]
        multi.sort(key=lambda x: -len(x[1]))

        return {
            "total_customers":         len(by_cust),
            "customers_with_multiple": len(multi),
            "total_repeat_conversations": sum(len(c) for _, c in multi),
            "top": [
                {
                    "customer_id": cid,
                    "task_count":  len(convs),
                    "chats": sum(1 for c in convs if c.type == "chat"),
                    "calls": sum(1 for c in convs if c.type == "call"),
                    "topics": sorted(set(c.topic for c in convs if c.topic))[:10],
                    "first":  min((c.created_at for c in convs if c.created_at), default=None).isoformat() if any(c.created_at for c in convs) else None,
                    "last":   max((c.created_at for c in convs if c.created_at), default=None).isoformat() if any(c.created_at for c in convs) else None,
                    "task_ids": [c.id for c in convs],
                }
                for cid, convs in multi[:30]
            ],
        }
    finally:
        db.close()


@app.get("/admin/crawl-runs")
def crawl_runs(limit: int = 30):
    """История запусков ночного парсера базы знаний."""
    db = SessionLocal()
    try:
        runs = db.query(DBCrawlRun).order_by(desc(DBCrawlRun.started_at)).limit(limit).all()
        active = db.query(DBDocument).filter(DBDocument.removed_at.is_(None)).count()
        removed = db.query(DBDocument).filter(DBDocument.removed_at.isnot(None)).count()
        return {
            "docs_active": active,
            "docs_removed": removed,
            "runs": [
                {
                    "id": r.id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "status": r.status,
                    "pages_total": r.pages_total,
                    "chars_total": r.chars_total,
                    "tables_total": r.tables_total,
                    "pages_added": r.pages_added,
                    "pages_updated": r.pages_updated,
                    "pages_removed": r.pages_removed,
                    "error_text": r.error_text,
                    "changes": r.log or [],
                }
                for r in runs
            ],
        }
    finally:
        db.close()


@app.get("/admin/kb-documents")
def kb_documents():
    """Текущий список документов базы знаний."""
    db = SessionLocal()
    try:
        docs = db.query(DBDocument).order_by(DBDocument.slug).all()
        return {
            "total": len(docs),
            "documents": [
                {
                    "slug": d.slug,
                    "title": d.title,
                    "url": d.url,
                    "chars": len(d.markdown or ""),
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                    "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                    "removed_at": d.removed_at.isoformat() if d.removed_at else None,
                }
                for d in docs
            ],
        }
    finally:
        db.close()


@app.post("/admin/reload-knowledge")
def reload_knowledge():
    """Перечитывает базу знаний из БД (зовётся после успешного ночного краула)."""
    global KNOWLEDGE_BASE, EVALUATOR
    KNOWLEDGE_BASE = load_knowledge_base()
    EVALUATOR = Evaluator(KNOWLEDGE_BASE)
    return {"ok": True, "chars": len(KNOWLEDGE_BASE)}


@app.post("/admin/pull-runs")
async def create_pull_run(request: Request):
    """Принимает сводку одной ночной выгрузки чатов (от pull_chats.py) и логирует её."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    db = SessionLocal()
    try:
        run = DBPullRun(
            started_at=_parse_dttm(body.get("started_at")) or datetime.utcnow(),
            finished_at=_parse_dttm(body.get("finished_at")) or datetime.utcnow(),
            status=(body.get("status") or "failed"),
            source=(body.get("source") or "launchd"),
            date_from=body.get("date_from"),
            date_to=body.get("date_to"),
            rows_exported=int(body.get("rows_exported") or 0),
            imported=int(body.get("imported") or 0),
            skipped_duplicates=int(body.get("skipped_duplicates") or 0),
            skipped_empty=int(body.get("skipped_empty") or 0),
            processed=int(body.get("processed") or 0),
            remaining=int(body.get("remaining") or 0),
            error_text=body.get("error_text"),
        )
        db.add(run)
        db.commit()
        return {"ok": True, "id": run.id}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


@app.get("/admin/pull-runs")
def list_pull_runs(limit: int = 30):
    """История ночных выгрузок чатов для дашборда."""
    db = SessionLocal()
    try:
        runs = (db.query(DBPullRun)
                  .order_by(DBPullRun.started_at.desc())
                  .limit(limit).all())
        return {"runs": [{
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "source": r.source,
            "date_from": r.date_from,
            "date_to": r.date_to,
            "rows_exported": r.rows_exported,
            "imported": r.imported,
            "skipped_duplicates": r.skipped_duplicates,
            "skipped_empty": r.skipped_empty,
            "processed": r.processed,
            "remaining": r.remaining,
            "error_text": r.error_text,
        } for r in runs]}
    finally:
        db.close()


@app.post("/admin/import-csv")
async def import_csv(request: Request):
    """Принимает сырой CSV-текст в теле запроса, парсит, создаёт строки conversations
    со status='pending'. AI-обработка — отдельным эндпоинтом."""
    import csv
    import io

    raw = (await request.body()).decode("utf-8", errors="replace")
    if not raw.strip():
        return {"error": "Empty body"}

    cohort = (request.query_params.get("cohort") or "").strip() or None

    reader = csv.DictReader(io.StringIO(raw))
    db = SessionLocal()
    created, skipped_empty, skipped_dup = 0, 0, 0
    tagged = 0
    try:
        for row in reader:
            task_id = (row.get("TASK_ID") or "").strip()
            if not task_id:
                continue
            chat_text = row.get("CHAT_TEXT") or ""
            turns = _parse_chat_text(chat_text)
            if not turns:
                skipped_empty += 1
                continue
            existing = db.query(DBConversation).filter_by(id=task_id).first()
            if existing:
                # Дозаполняем даты если их не было (например битый парсинг в прошлый раз)
                if existing.created_at is None:
                    existing.created_at = _parse_dttm(row.get("CREATED_DTTM"))
                if existing.in_progress_at is None:
                    existing.in_progress_at = _parse_dttm(row.get("IN_PROGRESS_DTTM"))
                if existing.closed_at is None:
                    existing.closed_at = _parse_dttm(row.get("CLOSED_DTTM"))
                # Тегируем существующее обращение меткой выборки (если задана и ещё не стоит)
                if cohort and existing.cohort != cohort:
                    existing.cohort = cohort
                    tagged += 1
                skipped_dup += 1
                continue

            queue = (row.get("QUEUE_NAME") or "").strip()
            conv_type = "call" if "call" in queue.lower() else "chat"

            # Имя оператора — из первой реплики агента
            agent_name = None
            for t in turns:
                if t["role"] == "agent":
                    agent_name = detect_agent_name(t["text"])
                    if agent_name:
                        break

            db.add(DBConversation(
                id=task_id,
                type=conv_type,
                queue_name=queue,
                customer_id=clean_customer_id(row.get("CUSTOMERID")),
                agent_name=agent_name,
                created_at=_parse_dttm(row.get("CREATED_DTTM")),
                in_progress_at=_parse_dttm(row.get("IN_PROGRESS_DTTM")),
                closed_at=_parse_dttm(row.get("CLOSED_DTTM")),
                transcript=turns,
                status="pending",
                cohort=cohort,
            ))
            created += 1
        db.commit()
    except Exception as e:
        db.rollback()
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()

    return {
        "imported": created,
        "skipped_empty_text": skipped_empty,
        "skipped_duplicates": skipped_dup,
        "tagged_cohort": tagged,
        "cohort": cohort,
        "note": "Run POST /admin/process-conversations to translate + score them.",
    }


def _process_one_conversation(conv_id: str):
    """Переводит turn'ы + классифицирует топик + оценивает одно обращение."""
    from sqlalchemy.orm.attributes import flag_modified
    db = SessionLocal()
    try:
        c = db.query(DBConversation).filter_by(id=conv_id).first()
        if not c or not c.transcript:
            return False

        # Перевод реплик
        transcript = list(c.transcript)
        for turn in transcript:
            if turn.get("text") and not turn.get("text_en"):
                t_en = translate_to_english(turn["text"])
                if t_en:
                    turn["text_en"] = t_en
        c.transcript = transcript
        flag_modified(c, "transcript")

        # Топик + оценка одним вызовом
        result = classify_and_evaluate_conversation(transcript)
        if result:
            slug = result.get("topic_slug")
            # Авторазметка НЕ перетирает ручную метку человека
            if slug and c.topic_source != "human":
                c.topic_slug = slug
                c.topic_source = "llm"
                c.topic_confidence = result.get("topic_confidence")
                c.product_line = result.get("product_line")
                c.direction = result.get("direction")
                name_en, name_es = topic_names(slug)
                c.topic = name_en or c.topic
                c.topic_es = name_es or c.topic_es
            c.avg_score = result.get("score")
            c.summary   = result.get("explanation")
            c.evaluation = result

        c.status = "done"
        db.commit()
        return True
    except Exception as e:
        print(f"[process-conv] {conv_id}: {e}")
        db.rollback()
        try:
            c = db.query(DBConversation).filter_by(id=conv_id).first()
            if c:
                c.status = "failed"
                db.commit()
        except Exception:
            pass
        return False
    finally:
        db.close()


@app.post("/admin/process-conversations")
def process_conversations(batch: int = 5):
    """Обрабатывает пачку pending-обращений (перевод + топик + оценка). Дёргать в цикле."""
    db = SessionLocal()
    try:
        pending = db.query(DBConversation).filter_by(status="pending").limit(batch).all()
        ids = [c.id for c in pending]
    finally:
        db.close()

    processed = 0
    for cid in ids:
        if _process_one_conversation(cid):
            processed += 1

    db = SessionLocal()
    try:
        remaining = db.query(func.count(DBConversation.id)).filter_by(status="pending").scalar() or 0
    finally:
        db.close()

    return {"processed_this_batch": processed, "remaining_pending": remaining, "done": remaining == 0}


# ══════════════════════════════════════════════════════════════════════════════
#  ТОПИКИ (управляемый словарь)
# ══════════════════════════════════════════════════════════════════════════════

def _editor_email(request: Request):
    u = getattr(request.state, "user", None)
    return u.get("email") if isinstance(u, dict) else None


@app.get("/topics")
def list_topics_endpoint(include_archived: bool = False):
    """Список топиков словаря + счётчики обращений (для дропдауна и банка топиков)."""
    db = SessionLocal()
    try:
        q = db.query(DBTopic)
        if not include_archived:
            q = q.filter(DBTopic.status == "active")
        topics = q.order_by(DBTopic.sort_order, DBTopic.id).all()
        counts = dict(db.query(DBConversation.topic_slug, func.count(DBConversation.id))
                        .filter(DBConversation.topic_slug.isnot(None))
                        .group_by(DBConversation.topic_slug).all())
        return [{
            "id": t.id, "slug": t.slug, "name_en": t.name_en, "name_es": t.name_es,
            "category": t.category, "description": t.description, "status": t.status,
            "sort_order": t.sort_order, "count": counts.get(t.slug, 0),
        } for t in topics]
    finally:
        db.close()


@app.post("/admin/topics")
async def create_or_update_topic(request: Request):
    """Создать/изменить топик. JSON: {id?, slug?, name_en, name_es, category, description, status}.
    Без id — создаёт новый (slug из name_en, если не задан); с id — обновляет."""
    body = await request.json()
    db = SessionLocal()
    try:
        tid = body.get("id")
        if tid:
            t = db.query(DBTopic).filter_by(id=tid).first()
            if not t:
                return {"error": "Topic not found"}
        else:
            slug = (body.get("slug") or "").strip()
            if not slug:
                slug = re.sub(r"[^a-z0-9]+", "_", (body.get("name_en") or "").lower()).strip("_")
            if not slug:
                return {"error": "name_en or slug required"}
            if db.query(DBTopic).filter_by(slug=slug).first():
                return {"error": f"slug '{slug}' already exists"}
            mx = db.query(func.max(DBTopic.sort_order)).scalar() or 0
            t = DBTopic(slug=slug, sort_order=mx + 1, created_by=_editor_email(request) or "manual")
            db.add(t)
        for f in ("name_en", "name_es", "category", "description", "status"):
            if body.get(f) is not None:
                setattr(t, f, body[f])
        db.commit()
        invalidate_topics_cache()
        return {"ok": True, "id": t.id, "slug": t.slug}
    finally:
        db.close()


@app.post("/admin/topics/{topic_id}/merge")
async def merge_topic(topic_id: int, request: Request):
    """Слить топик в другой: переназначить обращения и архивировать исходный. JSON: {into_slug}."""
    body = await request.json()
    into = (body.get("into_slug") or "").strip()
    db = SessionLocal()
    try:
        src = db.query(DBTopic).filter_by(id=topic_id).first()
        dst = db.query(DBTopic).filter_by(slug=into).first()
        if not src or not dst:
            return {"error": "source or target topic not found"}
        moved = db.query(DBConversation).filter_by(topic_slug=src.slug).update(
            {DBConversation.topic_slug: dst.slug, DBConversation.topic: dst.name_en,
             DBConversation.topic_es: dst.name_es}, synchronize_session=False)
        src.status = "archived"
        db.commit()
        invalidate_topics_cache()
        return {"ok": True, "moved": moved, "archived": src.slug, "into": dst.slug}
    finally:
        db.close()


@app.post("/conversations/{conv_id}/topic")
async def set_conversation_topic(conv_id: str, request: Request):
    """Ручная установка топика человеком (topic_source='human' — авторазметка её не перетирает)."""
    body = await request.json()
    slug = (body.get("topic_slug") or "").strip()
    db = SessionLocal()
    try:
        t = db.query(DBTopic).filter_by(slug=slug).first()
        if not t:
            return {"error": f"unknown topic_slug '{slug}'"}
        c = db.query(DBConversation).filter_by(id=conv_id).first()
        if not c:
            return {"error": "Conversation not found"}
        c.topic_slug = slug
        c.topic_source = "human"
        c.topic = t.name_en
        c.topic_es = t.name_es
        if body.get("product_line"):
            c.product_line = body["product_line"]
        if body.get("direction"):
            c.direction = body["direction"]
        db.commit()
        return {"ok": True, "topic_slug": slug, "topic": t.name_en, "topic_es": t.name_es}
    finally:
        db.close()


@app.get("/conversations/topic-stats")
def topic_stats(period: str = "week", cohort: Optional[str] = None, exclude_outbound: int = 0,
                segment: str = "all", from_date: Optional[str] = None, to_date: Optional[str] = None):
    """Тренды по топикам: текущее окно vs предыдущее равной длины + дельта.
    Пресеты period=day(1д)/week(7д)/month(30д) ИЛИ кастомный диапазон from_date..to_date (вкл.),
    тогда предыдущее окно — такой же длины непосредственно перед текущим.
    По каждому топику отдаём 2 метрики: число чатов и число уникальных пользователей.
    segment: all (все) | none (No Empresa account) | pfae (PFAE External/Golden) | pm (Persona Moral)."""
    now = datetime.utcnow()
    custom = False
    if from_date and to_date:
        try:
            fd = datetime.fromisoformat(from_date)
            td = datetime.fromisoformat(to_date)
            cur_start = datetime(fd.year, fd.month, fd.day)
            cur_end = datetime(td.year, td.month, td.day) + timedelta(days=1)  # to_date включительно
            span = max(1, (cur_end - cur_start).days)
            prev_start = cur_start - timedelta(days=span)
            custom = True
        except ValueError:
            custom = False
    if not custom:
        span = {"day": 1, "week": 7, "month": 30}.get(period, 7)
        cur_end = now
        cur_start = now - timedelta(days=span)
        prev_start = now - timedelta(days=2 * span)
    db = SessionLocal()
    try:
        tmap = {t["slug"]: t for t in load_topics()}
        q = db.query(DBConversation)
        if cohort:
            q = q.filter(DBConversation.cohort == cohort)
        else:
            q = q.filter(DBConversation.cohort.is_(None))
        q = q.filter(DBConversation.topic_slug.isnot(None),
                     DBConversation.merged_into.is_(None),   # фрагменты-продолжения не считаем
                     DBConversation.created_at.isnot(None),
                     DBConversation.created_at >= prev_start,
                     DBConversation.created_at < cur_end)
        if exclude_outbound:
            q = q.filter((DBConversation.direction != "outbound") | (DBConversation.direction.is_(None)))
        if segment == "pfae":
            q = q.filter(DBConversation.account_type.in_(["PFAE External", "PFAE Golden"]))
        elif segment == "pm":
            q = q.filter(DBConversation.account_type == "Persona Moral")
        elif segment == "none":
            q = q.filter(DBConversation.account_type == "No Empresa account")

        def slot(d, s):
            e = d.get(s)
            if not e:
                e = d[s] = {"chats": 0, "users": set()}
            return e

        cur, prev = {}, {}
        for c in q.all():
            e = slot(cur if c.created_at >= cur_start else prev, c.topic_slug)
            e["chats"] += 1
            cid = clean_customer_id(c.customer_id)
            if cid:
                e["users"].add(cid)
        items = []
        for s in (set(cur) | set(prev)):
            ce, pe = cur.get(s), prev.get(s)
            cc = ce["chats"] if ce else 0
            pc = pe["chats"] if pe else 0
            cu = len(ce["users"]) if ce else 0
            pu = len(pe["users"]) if pe else 0
            items.append({
                "slug": s, "topic": (tmap.get(s) or {}).get("name_en") or s,
                "category": (tmap.get(s) or {}).get("category"),
                "current": cc, "previous": pc, "delta": cc - pc,
                "users": cu, "users_prev": pu, "users_delta": cu - pu,
            })
        items.sort(key=lambda x: -x["current"])
        return {
            "period": "custom" if custom else period, "span_days": span,
            "current_from": cur_start.date().isoformat(),
            "current_to": (cur_end - timedelta(days=1)).date().isoformat(),
            "previous_from": prev_start.date().isoformat(),
            "topics": items,
        }
    finally:
        db.close()


@app.get("/admin/topic-suggestions")
def list_topic_suggestions(status: str = "pending"):
    """Предложения новых топиков от детектора."""
    db = SessionLocal()
    try:
        q = db.query(DBTopicSuggestion)
        if status:
            q = q.filter(DBTopicSuggestion.status == status)
        rows = q.order_by(desc(DBTopicSuggestion.count)).all()
        return [{
            "id": s.id, "proposed_name": s.proposed_name, "proposed_category": s.proposed_category,
            "rationale": s.rationale, "count": s.count, "period": s.period,
            "sample_conv_ids": s.sample_conv_ids, "status": s.status,
        } for s in rows]
    finally:
        db.close()


@app.post("/admin/detect-emerging-topics")
def detect_emerging_topics(days: int = 7, min_cluster: int = 3):
    """Кластеризует недавние необработанные обращения (proc_other / низкая уверенность)
    через DeepSeek и предлагает новые топики (TopicSuggestion)."""
    since = datetime.utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (db.query(DBConversation)
                  .filter(DBConversation.created_at >= since,
                          DBConversation.cohort.is_(None))
                  .filter((DBConversation.topic_slug == "proc_other") |
                          (DBConversation.topic_confidence < 0.5))
                  .all())
        items = []
        for c in rows:
            first = next((t.get("text_en") or t.get("text") for t in (c.transcript or [])
                          if t.get("role") == "customer" and (t.get("text_en") or t.get("text"))), None)
            if first:
                items.append((c.id, first[:200]))
    finally:
        db.close()
    period = f"last {days}d"
    if len(items) < min_cluster:
        return {"ok": True, "analyzed": len(items), "suggested": 0, "note": "too few unclassified contacts"}

    catalog = ", ".join(sorted(t["slug"] for t in load_topics()))
    sample = "\n".join(f"{i}. {txt}" for i, (cid, txt) in enumerate(items[:120]))
    prompt = f"""Eres analista de soporte de Banco Plata PyME. Estos contactos NO encajaron bien en el catálogo actual de temas.
Catálogo actual (slugs existentes, NO los repitas): {catalog}

CONTACTOS SIN CLASIFICAR:
{sample}

Agrupa estos contactos en POSIBLES TEMAS NUEVOS que NO estén en el catálogo. Propón un tema solo si tiene al menos {min_cluster} contactos similares.
Responde SOLO JSON: {{"suggestions":[{{"name_en":"<nombre corto del tema>","category":"<una de las 8 categorías>","count":<n>,"rationale":"<por qué, 1 frase>","example_indices":[<i>,...]}}]}}"""
    try:
        resp = EVALUATOR_CLIENT.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}],
            max_tokens=800, temperature=0.2)
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```json\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        suggestions = json.loads(content).get("suggestions", [])
    except Exception as e:
        return {"error": f"DeepSeek: {e}"}

    created = 0
    db = SessionLocal()
    try:
        for s in suggestions:
            name = (s.get("name_en") or "").strip()
            if not name or (s.get("count") or 0) < min_cluster:
                continue
            if db.query(DBTopicSuggestion).filter_by(proposed_name=name, status="pending").first():
                continue
            idxs = s.get("example_indices") or []
            samples = [items[i][0] for i in idxs if isinstance(i, int) and 0 <= i < len(items)][:8]
            db.add(DBTopicSuggestion(
                proposed_name=name, proposed_category=s.get("category"),
                rationale=s.get("rationale"), count=int(s.get("count") or 0),
                period=period, sample_conv_ids=samples, status="pending"))
            created += 1
        db.commit()
    finally:
        db.close()
    return {"ok": True, "analyzed": len(items), "suggested": created}


@app.post("/admin/topic-suggestions/{sug_id}/accept")
def accept_topic_suggestion(sug_id: int):
    """Принять предложение → создать топик из него."""
    db = SessionLocal()
    try:
        s = db.query(DBTopicSuggestion).filter_by(id=sug_id).first()
        if not s:
            return {"error": "not found"}
        slug = re.sub(r"[^a-z0-9]+", "_", (s.proposed_name or "").lower()).strip("_") or f"topic_{sug_id}"
        if not db.query(DBTopic).filter_by(slug=slug).first():
            mx = db.query(func.max(DBTopic.sort_order)).scalar() or 0
            db.add(DBTopic(slug=slug, name_en=s.proposed_name, name_es=s.proposed_name,
                           category=s.proposed_category, description=s.rationale,
                           status="active", sort_order=mx + 1, created_by="suggestion"))
        s.status = "accepted"
        db.commit()
        invalidate_topics_cache()
        return {"ok": True, "slug": slug}
    finally:
        db.close()


@app.post("/admin/topic-suggestions/{sug_id}/reject")
def reject_topic_suggestion(sug_id: int):
    db = SessionLocal()
    try:
        s = db.query(DBTopicSuggestion).filter_by(id=sug_id).first()
        if s:
            s.status = "rejected"
            db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.get("/metrics/weekly")
def weekly_metrics(cohort: Optional[str] = None, week_offset: int = 0,
                   from_date: Optional[str] = None, to_date: Optional[str] = None):
    """KPI за период vs предыдущий равной длины: чаты/пользователи (всего и PFAE)
    + среднее число тасок на пользователя.
    По умолчанию — текущая неделя (Пн–Вс). week_offset=N сдвигает окно на N недель назад.
    from_date..to_date (вкл.) — кастомный период, дельта к такому же предыдущему."""
    now = datetime.utcnow()
    custom = False
    if from_date and to_date:
        try:
            fd = datetime.fromisoformat(from_date)
            td = datetime.fromisoformat(to_date)
            week_start = datetime(fd.year, fd.month, fd.day)
            week_end = datetime(td.year, td.month, td.day) + timedelta(days=1)  # to_date включительно
            custom = True
        except ValueError:
            custom = False
    if not custom:
        today = now.date()
        ws_d = today - timedelta(days=today.weekday()) - timedelta(weeks=max(0, week_offset))
        week_start = datetime(ws_d.year, ws_d.month, ws_d.day)
        week_end = week_start + timedelta(days=7)
    span = week_end - week_start
    prev_start = week_start - span
    PFAE = ("PFAE External", "PFAE Golden")
    db = SessionLocal()
    try:
        q = db.query(DBConversation).filter(
            DBConversation.created_at.isnot(None),
            DBConversation.merged_into.is_(None),   # фрагменты-продолжения не считаем
            DBConversation.created_at >= prev_start,
            DBConversation.created_at < week_end)
        if cohort:
            q = q.filter(DBConversation.cohort == cohort)
        else:
            q = q.filter(DBConversation.cohort.is_(None))
        cur = {"chats": 0, "users": set(), "pfae_chats": 0, "pfae_users": set()}
        prev = {"chats": 0, "users": set(), "pfae_chats": 0, "pfae_users": set()}
        for c in q.all():
            b = cur if c.created_at >= week_start else prev
            b["chats"] += 1
            cid = clean_customer_id(c.customer_id)
            if cid:
                b["users"].add(cid)
            if c.account_type in PFAE:
                b["pfae_chats"] += 1
                if cid:
                    b["pfae_users"].add(cid)

        def metric(key, is_set=False):
            cv = len(cur[key]) if is_set else cur[key]
            pv = len(prev[key]) if is_set else prev[key]
            return {"value": cv, "prev": pv, "delta": cv - pv}

        def avg_metric():
            cu, pu = len(cur["users"]), len(prev["users"])
            cv = round(cur["chats"] / cu, 1) if cu else 0
            pv = round(prev["chats"] / pu, 1) if pu else 0
            return {"value": cv, "prev": pv, "delta": round(cv - pv, 1)}

        return {
            "week_start": week_start.date().isoformat(),
            "week_end": (week_end - timedelta(days=1)).date().isoformat(),
            "period": "custom" if custom else "week",
            "week_offset": 0 if custom else max(0, week_offset),
            "chats": metric("chats"),
            "users": metric("users", True),
            "pfae_chats": metric("pfae_chats"),
            "pfae_users": metric("pfae_users", True),
            "avg_per_user": avg_metric(),
        }
    finally:
        db.close()


@app.post("/admin/user-accounts")
async def set_user_accounts(request: Request):
    """Bulk-апдейт account_type по customer_id (из ночного funnel-запроса).
    Тело JSON: {"accounts": [{"customer_id": "...", "account_type": "PFAE External|PFAE Golden|Persona Moral|No Empresa account", "tariff": "..."}]}.
    Проставляет conversations.account_type/tariff всем обращениям пользователя."""
    body = await request.json()
    accounts = body.get("accounts") or []
    db = SessionLocal()
    updated = users = 0
    try:
        for a in accounts:
            cid = (a.get("customer_id") or "").strip()
            label = a.get("account_type")
            if not cid or not label:
                continue
            n = db.query(DBConversation).filter(DBConversation.customer_id == cid).update(
                {DBConversation.account_type: label, DBConversation.tariff: a.get("tariff")},
                synchronize_session=False)
            updated += n
            users += 1
        db.commit()
        return {"ok": True, "users": users, "rows_updated": updated}
    finally:
        db.close()


def _cust_msgs(c):
    """Число содержательных реплик КЛИЕНТА в чате."""
    return sum(1 for t in (c.transcript or [])
               if t.get("role") == "customer" and (t.get("text") or "").strip())


def _link_fragments(db, gap_minutes: int = 10, thin_cust_msgs: int = 2):
    """Склейка коротких чатов-продолжений в основной диалог.
    Когда саппорт долго не отвечает, разговор рвётся на несколько чатов. Логика:
    чаты ОДНОГО клиента за ОДИН день, идущие подряд с разрывом ≤ gap_minutes, образуют
    кластер; «основной» = чат с макс. числом реплик; остальные ТОНКИЕ (≤ thin_cust_msgs
    реплик клиента) помечаются merged_into = id основного. Пересчёт целиком (идемпотентно)."""
    from collections import defaultdict
    rows = (db.query(DBConversation)
              .filter(DBConversation.type == "chat",
                      DBConversation.customer_id.isnot(None),
                      DBConversation.created_at.isnot(None))
              .all())
    by_cust = defaultdict(list)
    for c in rows:
        by_cust[c.customer_id].append(c)
    gap = timedelta(minutes=gap_minutes)
    assign = {}  # fragment_id -> main_id

    def flush(cluster):
        if len(cluster) < 2:
            return
        # основной — с наибольшим числом реплик (при равенстве — более ранний)
        main = sorted(cluster, key=lambda c: (-len(c.transcript or []), c.created_at))[0]
        for c in cluster:
            if c.id != main.id and _cust_msgs(c) <= thin_cust_msgs:
                assign[c.id] = main.id

    for cust, lst in by_cust.items():
        lst.sort(key=lambda c: c.created_at)
        cluster, prev = [], None
        for c in lst:
            if prev is not None and (c.created_at - (prev.closed_at or prev.created_at)) <= gap \
               and c.created_at.date() == prev.created_at.date():
                cluster.append(c)
            else:
                flush(cluster); cluster = [c]
            prev = c
        flush(cluster)

    # полный пересчёт: сбрасываем старые метки, ставим новые
    db.query(DBConversation).filter(DBConversation.merged_into.isnot(None)).update(
        {DBConversation.merged_into: None}, synchronize_session=False)
    for fid, pid in assign.items():
        db.query(DBConversation).filter(DBConversation.id == fid).update(
            {DBConversation.merged_into: pid}, synchronize_session=False)
    db.commit()
    return len(assign)


@app.post("/admin/link-fragments")
def admin_link_fragments(gap_minutes: int = 10, thin_cust_msgs: int = 2):
    """Пересчитать склейку фрагментов-продолжений. Вызывается пуллером после обработки."""
    db = SessionLocal()
    try:
        n = _link_fragments(db, gap_minutes, thin_cust_msgs)
        return {"ok": True, "merged": n}
    finally:
        db.close()


# ─────────── Семантическая группировка диалогов одного клиента ───────────

def cluster_customer_chats(convs: list) -> list:
    """LLM читает все чаты одного клиента за день и группирует их по СМЫСЛУ:
    несколько чатов одного обращения (саппорт не ответил >15 мин / клиент пропал и открыл
    новый чат) → один кластер; разные темы → разные кластеры.
    Возвращает list[list[conv]]. Фолбэк при сбое LLM — группировка по совпадению topic_slug."""
    if len(convs) < 2:
        return [[c] for c in convs]
    lines = []
    for i, c in enumerate(convs):
        cmsgs = [t.get("text", "") for t in (c.transcript or [])
                 if t.get("role") == "customer" and (t.get("text") or "").strip()]
        snippet = " | ".join(cmsgs[:4])[:300] or "(sin mensajes del cliente)"
        tm = c.created_at.strftime("%H:%M") if c.created_at else "--:--"
        lines.append(f"[{i}] {tm} (tema: {c.topic_slug or '-'}): {snippet}")
    body = "\n".join(lines)
    prompt = f"""Eres analista de soporte de Banco Plata. Un mismo cliente abrió VARIOS chats el mismo día.
Con frecuencia es la MISMA conversación partida en varios chats (el agente tardó más de 15 min o el cliente dejó de responder y se abrió un chat nuevo). Pero a veces son asuntos DISTINTOS.

Agrupa los índices de los chats que pertenecen al MISMO asunto/conversación. Chats de asuntos diferentes van en grupos separados. Cada índice debe aparecer EXACTAMENTE una vez.

CHATS DEL CLIENTE (índice, hora, tema actual, primeros mensajes del cliente):
{body}

Responde SOLO con JSON, sin markdown: {{"groups": [[0,2],[1],...]}}"""
    try:
        resp = EVALUATOR_CLIENT.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}],
            max_tokens=300, temperature=0.1)
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```json\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        groups = json.loads(content).get("groups", [])
        seen, out = set(), []
        for g in groups:
            cl = [convs[i] for i in g if isinstance(i, int) and 0 <= i < len(convs) and i not in seen]
            for i in g:
                if isinstance(i, int):
                    seen.add(i)
            if cl:
                out.append(cl)
        for i, c in enumerate(convs):     # пропущенные индексы → standalone
            if i not in seen:
                out.append([c])
        return out or [[c] for c in convs]
    except Exception as e:
        print(f"[cluster] {e} — фолбэк по topic_slug")
        from collections import defaultdict
        byt = defaultdict(list)
        for c in convs:
            byt[c.topic_slug or "_"].append(c)
        return list(byt.values())


def _group_conversations(db, from_date: Optional[str] = None, to_date: Optional[str] = None,
                         min_group: int = 2):
    """Группирует чаты одного клиента за один день по смыслу (LLM), объединённые треды
    помечает merged_into = id «основного» (самого раннего) и ПЕРЕОЦЕНИВАЕТ объединённый
    транскрипт целиком. Пересчёт в пределах окна дат (по умолчанию — вся база)."""
    from collections import defaultdict
    from sqlalchemy.orm.attributes import flag_modified
    q = db.query(DBConversation).filter(
        DBConversation.type == "chat",
        DBConversation.customer_id.isnot(None),
        DBConversation.created_at.isnot(None))
    if from_date and to_date:
        try:
            fd = datetime.fromisoformat(from_date); td = datetime.fromisoformat(to_date)
            lo = datetime(fd.year, fd.month, fd.day)
            hi = datetime(td.year, td.month, td.day) + timedelta(days=1)
            q = q.filter(DBConversation.created_at >= lo, DBConversation.created_at < hi)
        except ValueError:
            pass
    rows = q.all()
    # сброс прежних меток в пределах окна
    for c in rows:
        c.merged_into = None
    by_cd = defaultdict(list)
    for c in rows:
        by_cd[(c.customer_id, c.created_at.date())].append(c)

    merged = groups_formed = reevaluated = 0
    for (cust, day), lst in by_cd.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda c: c.created_at)
        for cl in cluster_customer_chats(lst):
            if len(cl) < min_group:
                continue
            cl.sort(key=lambda c: c.created_at)
            primary = cl[0]
            for c in cl[1:]:
                c.merged_into = primary.id
                merged += 1
            groups_formed += 1
            # объединённый транскрипт всех тасок треда → переоценка целиком
            combined = []
            for c in cl:
                for t in (c.transcript or []):
                    if t.get("text"):
                        combined.append({"role": t["role"], "text": t["text"],
                                         "text_en": t.get("text_en")})
            res = classify_and_evaluate_conversation(combined)
            if res:
                slug = res.get("topic_slug")
                if slug and primary.topic_source != "human":
                    primary.topic_slug = slug
                    primary.topic_source = "llm"
                    primary.topic_confidence = res.get("topic_confidence")
                    primary.product_line = res.get("product_line")
                    primary.direction = res.get("direction")
                    ne, ns = topic_names(slug)
                    primary.topic = ne or primary.topic
                    primary.topic_es = ns or primary.topic_es
                primary.avg_score = res.get("score")
                primary.summary = res.get("explanation")
                primary.evaluation = res
                reevaluated += 1
        db.commit()
    db.commit()
    return {"merged": merged, "groups": groups_formed, "reevaluated": reevaluated}


@app.post("/admin/group-conversations")
def admin_group_conversations(from_date: Optional[str] = None, to_date: Optional[str] = None):
    """Семантическая группировка чатов клиента в объединённые диалоги + переоценка.
    Вызывается пуллером после обработки (с окном выгрузки). Без дат — по всей базе."""
    db = SessionLocal()
    try:
        return {"ok": True, **_group_conversations(db, from_date, to_date)}
    finally:
        db.close()


# ─────────── Серверный self-heal: сам добивает pending независимо от пуллера ───────────
# Локальный пуллер иногда обрывается на середине обработки (сеть/сон Mac) и оставляет
# необработанные обращения. Этот фоновой воркер на web дообрабатывает их сам, а после
# полного дренажа запускает семантическую группировку по затронутым дням. Так обработка
# перестаёт зависеть от локальной машины. Отключается env PENDING_WORKER=0.

_worker_started = False


def _pending_worker_loop():
    dirty_days = set()
    while True:
        try:
            db = SessionLocal()
            ids = [c.id for c in db.query(DBConversation.id.label("id"))
                     .filter(DBConversation.status == "pending")
                     .order_by(DBConversation.created_at).limit(5).all()]
            db.close()
            if not ids:
                # нечего обрабатывать: если что-то доделали — группируем ОКНО последних 4 дней
                # (не только дни, чьи pending дренажили сами) — чтобы дни, обработанные
                # пуллером но пропустившие группировку (напр. при сбое), тоже сгруппировались.
                if dirty_days:
                    gdb = SessionLocal()
                    try:
                        d_to = datetime.fromisoformat(max(dirty_days)).date()
                        d_from = (d_to - timedelta(days=3)).isoformat()
                        r = _group_conversations(gdb, d_from, d_to.isoformat())
                        print(f"[pending-worker] дренаж завершён, группировка окна "
                              f"{d_from}..{d_to}: {r}")
                    except Exception as ge:
                        print(f"[pending-worker] group window: {ge}")
                    finally:
                        gdb.close()
                    dirty_days.clear()
                time.sleep(60)
                continue
            # обработать батч
            gdb = SessionLocal()
            for c in gdb.query(DBConversation).filter(DBConversation.id.in_(ids)).all():
                if c.created_at:
                    dirty_days.add(c.created_at.date().isoformat())
            gdb.close()
            for cid in ids:
                _process_one_conversation(cid)
            time.sleep(1)
        except Exception as e:
            print(f"[pending-worker] {e}")
            time.sleep(60)


def _ind_translate_worker_loop():
    """Серверный воркер перевода диалогов физиков (Individuals): переводит ES→EN
    батчами прямо на Railway (без зависимости от локальной машины). Спит, когда всё
    переведено. Трудные (не выровнялись) помечает translated, чтобы не зацикливаться."""
    while True:
        try:
            db = SessionLocal()
            try:
                q = db.query(DBIndividual).filter(DBIndividual.status != "translated").limit(6)
                try:
                    pend = q.with_for_update(skip_locked=True).all()
                except Exception:
                    pend = q.all()
                if not pend:
                    time.sleep(120)
                    continue
                for d in pend:
                    if not _translate_individual(d):
                        d.status = "translated"   # не зацикливаемся — оставим ES
                db.commit()
            finally:
                db.close()
            time.sleep(1)
        except Exception as e:
            print(f"[ind-translate-worker] {e}")
            time.sleep(60)


@app.on_event("startup")
def _start_pending_worker():
    global _worker_started
    if _worker_started or os.getenv("PENDING_WORKER", "1") != "1":
        return
    _worker_started = True
    threading.Thread(target=_pending_worker_loop, daemon=True).start()
    print("[pending-worker] запущен (self-heal обработки)")
    if os.getenv("IND_TRANSLATE_WORKER", "1") == "1":
        threading.Thread(target=_ind_translate_worker_loop, daemon=True).start()
        print("[ind-translate-worker] запущен (перевод физиков)")


@app.get("/conversations")
def list_conversations(limit: int = 500, cohort: Optional[str] = None,
                       topic_slug: Optional[str] = None, days: Optional[int] = None,
                       exclude_outbound: int = 0, customer_id: Optional[str] = None,
                       from_date: Optional[str] = None, to_date: Optional[str] = None,
                       include_merged: int = 0):
    """Список реальных обращений (последние первые).
    cohort=<метка> — только эта выборка; без параметра — обычный Real Inbox (без когорт).
    topic_slug/days/from_date/to_date/exclude_outbound/customer_id — фильтры (drill-down)."""
    db = SessionLocal()
    try:
        q = db.query(DBConversation)
        if customer_id:
            # по конкретному пользователю — игнорируем фильтр когорт; фрагменты показываем (помечены в UI)
            q = q.filter(DBConversation.customer_id == customer_id)
        else:
            if cohort:
                q = q.filter(DBConversation.cohort == cohort)
            else:
                q = q.filter(DBConversation.cohort.is_(None))
            if not include_merged:
                # в общих списках/drill-down фрагменты-продолжения скрываем (свёрнуты в основной)
                q = q.filter(DBConversation.merged_into.is_(None))
        if topic_slug:
            q = q.filter(DBConversation.topic_slug == topic_slug)
        if from_date and to_date:
            try:
                fd = datetime.fromisoformat(from_date); td = datetime.fromisoformat(to_date)
                q = q.filter(DBConversation.created_at >= datetime(fd.year, fd.month, fd.day),
                             DBConversation.created_at < datetime(td.year, td.month, td.day) + timedelta(days=1))
            except ValueError:
                pass
        elif days:
            q = q.filter(DBConversation.created_at >= datetime.utcnow() - timedelta(days=days))
        if exclude_outbound:
            q = q.filter((DBConversation.direction != "outbound") | (DBConversation.direction.is_(None)))
        rows = q.order_by(desc(DBConversation.created_at)).limit(limit).all()
        ids = [c.id for c in rows]
        child_counts = {}
        if ids:
            for pid, cnt in (db.query(DBConversation.merged_into, func.count())
                               .filter(DBConversation.merged_into.in_(ids))
                               .group_by(DBConversation.merged_into).all()):
                child_counts[pid] = cnt
        return [{
            "id":          c.id,
            "type":        c.type,
            "customer_id": c.customer_id,
            "agent_name":  c.agent_name,
            "topic":       c.topic,
            "topic_es":    c.topic_es,
            "topic_slug":  c.topic_slug,
            "topic_source": c.topic_source,
            "topic_confidence": c.topic_confidence,
            "product_line": c.product_line,
            "direction":   c.direction,
            "account_type": c.account_type,
            "tariff":      c.tariff,
            "avg_score":   c.avg_score,
            "status":      c.status,
            "cohort":      c.cohort,
            "created_at":  c.created_at.isoformat() if c.created_at else None,
            "closed_at":   c.closed_at.isoformat() if c.closed_at else None,
            "turns":       len(c.transcript or []),
            "merged_into": c.merged_into,
            "child_count": child_counts.get(c.id, 0),
        } for c in rows]
    finally:
        db.close()


@app.get("/conversations/stats")
def conversations_stats(cohort: Optional[str] = None):
    """Статистика реальных обращений по дням + топ топиков.
    cohort=<метка> — только эта выборка; без параметра — обычный Real Inbox (без когорт)."""
    db = SessionLocal()
    try:
        q = db.query(DBConversation).filter(DBConversation.merged_into.is_(None))  # фрагменты не считаем
        if cohort:
            q = q.filter(DBConversation.cohort == cohort)
        else:
            q = q.filter(DBConversation.cohort.is_(None))
        rows = q.all()
        tmap = {t["slug"]: t for t in load_topics()}
        by_day = {}
        slug_counts = {}            # topic_slug -> count
        legacy_topics = {}          # фолбэк по свободному topic (для старых не-размеченных)
        cat_counts = {}
        prod_counts = {}
        dir_counts = {}
        total_chat = total_call = 0
        scores = []
        customers = set()
        done = 0
        for c in rows:
            if c.type == "call": total_call += 1
            else:                total_chat += 1
            cust = clean_customer_id(c.customer_id)
            if cust:
                customers.add(cust)
            if c.status == "done":
                done += 1
            if c.avg_score is not None:
                scores.append(c.avg_score)
            if c.created_at:
                day = c.created_at.date().isoformat()
                d = by_day.setdefault(day, {"date": day, "chat": 0, "call": 0, "total": 0})
                d[c.type] = d.get(c.type, 0) + 1
                d["total"] += 1
            if c.topic_slug:
                slug_counts[c.topic_slug] = slug_counts.get(c.topic_slug, 0) + 1
                cat = (tmap.get(c.topic_slug) or {}).get("category") or "—"
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
            elif c.topic:
                legacy_topics[c.topic] = legacy_topics.get(c.topic, 0) + 1
            if c.product_line:
                prod_counts[c.product_line] = prod_counts.get(c.product_line, 0) + 1
            if c.direction:
                dir_counts[c.direction] = dir_counts.get(c.direction, 0) + 1

        days = sorted(by_day.values(), key=lambda x: x["date"])
        # топ по управляемым топикам (резолвим имена); фолбэк на legacy если slug-ов ещё нет
        if slug_counts:
            top = sorted(slug_counts.items(), key=lambda x: -x[1])[:25]
            top_topics = [{
                "slug": s,
                "topic": (tmap.get(s) or {}).get("name_en") or s,
                "topic_es": (tmap.get(s) or {}).get("name_es") or s,
                "category": (tmap.get(s) or {}).get("category"),
                "count": n,
            } for s, n in top]
        else:
            top = sorted(legacy_topics.items(), key=lambda x: -x[1])[:25]
            top_topics = [{"slug": None, "topic": t, "topic_es": t, "category": None, "count": n} for t, n in top]
        return {
            "total": len(rows),
            "total_chat": total_chat,
            "total_call": total_call,
            "customers": len(customers),
            "processed": done,
            "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
            "by_day": days,
            "top_topics": top_topics,
            "top_topics_es": [{"topic": t["topic_es"], "count": t["count"]} for t in top_topics],
            "by_category": sorted([{"category": k, "count": v} for k, v in cat_counts.items()], key=lambda x: -x["count"]),
            "by_product": prod_counts,
            "by_direction": dir_counts,
        }
    finally:
        db.close()


@app.get("/conversations/{conv_id}")
def get_conversation(conv_id: str):
    """Полные данные обращения + история этого же customer_id."""
    db = SessionLocal()
    try:
        c = db.query(DBConversation).filter_by(id=conv_id).first()
        if not c:
            return {"error": "Conversation not found"}

        # История этого клиента: все остальные его обращения (только при реальном customer_id)
        customer_history = []
        cust_id = clean_customer_id(c.customer_id)
        if cust_id:
            others = db.query(DBConversation).filter(
                DBConversation.customer_id == cust_id,
                DBConversation.id != c.id,
            ).order_by(desc(DBConversation.created_at)).all()
            customer_history = [{
                "id":         o.id,
                "type":       o.type,
                "topic":      o.topic,
                "topic_es":   o.topic_es,
                "avg_score":  o.avg_score,
                "agent_name": o.agent_name,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            } for o in others]

        # таски, объединённые в этот диалог (merged_into = c.id)
        child_objs = (db.query(DBConversation).filter(DBConversation.merged_into == c.id)
                        .order_by(DBConversation.created_at).all())
        children = [{
            "id": ch.id, "created_at": ch.created_at.isoformat() if ch.created_at else None,
            "turns": len(ch.transcript or []), "topic": ch.topic,
        } for ch in child_objs]

        # объединённый транскрипт всего треда (основной + приклеенные), по времени;
        # перед каждой таской — маркер-разделитель с её временем
        members = sorted([c] + child_objs, key=lambda m: m.created_at or datetime.min)
        combined_transcript = []
        for idx, m in enumerate(members):
            combined_transcript.append({
                "boundary": True, "task_id": m.id,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "part": idx + 1,
            })
            for t in (m.transcript or []):
                if t.get("text"):
                    combined_transcript.append({"role": t.get("role"), "text": t.get("text"),
                                                "text_en": t.get("text_en")})
        task_count = len(members)

        return {
            "id":          c.id,
            "type":        c.type,
            "queue_name":  c.queue_name,
            "customer_id": cust_id,
            "agent_name":  c.agent_name,
            "topic":       c.topic,
            "topic_es":    c.topic_es,
            "topic_slug":  c.topic_slug,
            "topic_source": c.topic_source,
            "topic_confidence": c.topic_confidence,
            "product_line": c.product_line,
            "direction":   c.direction,
            "account_type": c.account_type,
            "tariff":      c.tariff,
            "avg_score":   c.avg_score,
            "evaluation":  c.evaluation,
            "summary":     c.summary,
            "status":      c.status,
            "transcript":  c.transcript or [],
            "created_at":  c.created_at.isoformat() if c.created_at else None,
            "in_progress_at": c.in_progress_at.isoformat() if c.in_progress_at else None,
            "closed_at":   c.closed_at.isoformat() if c.closed_at else None,
            "customer_history": customer_history,
            "merged_into": c.merged_into,
            "children":    children,
            "task_count":  task_count,
            "combined_transcript": combined_transcript,
        }
    finally:
        db.close()


@app.get("/customers")
def list_customers(q: Optional[str] = None, limit: int = 300):
    """Список пользователей (по customer_id) с агрегатами. q — поиск по подстроке id."""
    db = SessionLocal()
    try:
        query = db.query(DBConversation).filter(DBConversation.customer_id.isnot(None))
        if q:
            query = query.filter(DBConversation.customer_id.ilike(f"%{q.strip()}%"))
        rows = query.all()
        agg = {}
        for c in rows:
            cid = clean_customer_id(c.customer_id)
            if not cid:
                continue
            a = agg.setdefault(cid, {"customer_id": cid, "count": 0, "first": None,
                                     "last": None, "topics": {}, "scores": [], "account_type": None, "tariff": None})
            a["count"] += 1
            if c.account_type:
                a["account_type"] = c.account_type
            if c.tariff:
                a["tariff"] = c.tariff
            if c.created_at:
                iso = c.created_at.isoformat()
                if a["first"] is None or iso < a["first"]:
                    a["first"] = iso
                if a["last"] is None or iso > a["last"]:
                    a["last"] = iso
            if c.topic_slug:
                a["topics"][c.topic_slug] = a["topics"].get(c.topic_slug, 0) + 1
            if c.avg_score is not None:
                a["scores"].append(c.avg_score)
        out = []
        for a in agg.values():
            top = max(a["topics"].items(), key=lambda x: x[1])[0] if a["topics"] else None
            out.append({
                "customer_id": a["customer_id"], "count": a["count"],
                "first_at": a["first"], "last_at": a["last"], "top_topic": top,
                "avg_score": round(sum(a["scores"]) / len(a["scores"]), 1) if a["scores"] else None,
                "account_type": a["account_type"], "tariff": a["tariff"],
            })
        out.sort(key=lambda x: (-x["count"], x["last_at"] or ""))
        return {"total": len(out), "customers": out[:limit]}
    finally:
        db.close()


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    """Профиль пользователя: все его обращения (даты-блоки) + сводка."""
    db = SessionLocal()
    try:
        tmap = {t["slug"]: t for t in load_topics()}
        rows = (db.query(DBConversation)
                  .filter(DBConversation.customer_id == customer_id)
                  .order_by(desc(DBConversation.created_at)).all())
        convs = [{
            "id": c.id, "type": c.type, "topic": c.topic, "topic_slug": c.topic_slug,
            "topic_name": (tmap.get(c.topic_slug) or {}).get("name_en") or c.topic,
            "product_line": c.product_line, "direction": c.direction,
            "avg_score": c.avg_score, "agent_name": c.agent_name, "status": c.status,
            "turns": len(c.transcript or []),
            "transcript": c.transcript or [],
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "merged_into": c.merged_into,
        } for c in rows]
        # агрегаты (count/topics/avg) считаем без фрагментов-продолжений; сами фрагменты в треде показываем
        main_rows = [c for c in rows if not c.merged_into]
        scores = [c.avg_score for c in main_rows if c.avg_score is not None]
        topics = {}
        for c in main_rows:
            if c.topic_slug:
                topics[c.topic_slug] = topics.get(c.topic_slug, 0) + 1
        account_type = next((c.account_type for c in rows if c.account_type), None)
        tariff = next((c.tariff for c in rows if c.tariff), None)
        return {
            "customer_id": customer_id, "count": len(main_rows),
            "account_type": account_type, "tariff": tariff,
            "first_at": convs[-1]["created_at"] if convs else None,
            "last_at": convs[0]["created_at"] if convs else None,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "topics": [{"slug": s, "name": (tmap.get(s) or {}).get("name_en") or s, "count": n}
                       for s, n in sorted(topics.items(), key=lambda x: -x[1])],
            "conversations": convs,
        }
    finally:
        db.close()


# ═══════════════════ INDIVIDUALS (клиенты-физики) — отдельная линия ═══════════════════
# Только отображение: чат ES+EN, ссылка, продуктовые флаги. Без топиков/оценки.

def _translate_individual(dlg) -> bool:
    """Переводит транскрипт одного диалога физика (ES→EN) одним вызовом DeepSeek,
    выравнивая по номерам строк. Пишет text_en в реплики. Возвращает True при успехе."""
    from sqlalchemy.orm.attributes import flag_modified
    turns = list(dlg.transcript or [])
    idxs = [i for i, t in enumerate(turns) if (t.get("text") or "").strip() and not t.get("text_en")]
    if not idxs:
        dlg.status = "translated"
        return True
    numbered = "\n".join(f"{j}. {turns[i]['text']}" for j, i in enumerate(idxs))
    prompt = ("Translate each numbered Spanish line to natural English. "
              "Return ONLY a JSON array of strings, same length and order, no numbering.\n\n" + numbered)
    try:
        resp = EVALUATOR_CLIENT.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}],
            max_tokens=4000, temperature=0.1)
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```json\s*", "", content); content = re.sub(r"\s*```$", "", content)
        arr = json.loads(content)
        if isinstance(arr, list) and len(arr) == len(idxs):
            for j, i in enumerate(idxs):
                turns[i]["text_en"] = str(arr[j])
            dlg.transcript = turns
            flag_modified(dlg, "transcript")
            dlg.status = "translated"
            return True
    except Exception as e:
        print(f"[ind-translate] {dlg.id}: {e}")
    return False


@app.post("/admin/individuals/translate")
def admin_individuals_translate(batch: int = 5):
    """Переводит пачку непереведённых диалогов физиков. Гоняется циклом до конца."""
    db = SessionLocal()
    try:
        # claim distinct rows so несколько параллельных воркеров не берут одни и те же
        q = db.query(DBIndividual).filter(DBIndividual.status != "translated").limit(batch)
        try:
            pend = q.with_for_update(skip_locked=True).all()
        except Exception:
            pend = q.all()   # sqlite / нет поддержки — fallback
        done = 0
        for d in pend:
            if _translate_individual(d):
                done += 1
        db.commit()
        remaining = db.query(func.count(DBIndividual.id)).filter(DBIndividual.status != "translated").scalar() or 0
        return {"translated_this_batch": done, "remaining": remaining, "done": remaining == 0}
    finally:
        db.close()


def _ind_products(p):
    """Список активных продуктовых флагов физика для бейджей."""
    if not p:
        return []
    names = {"cc": "Credit card", "dc": "Debit", "garantizada": "Garantizada",
             "plata_plus": "Plata+", "inv": "Investments", "cl": "Loan", "pyme": "PyME"}
    return [names[k] for k in names if p.get(k) is True]


@app.get("/individuals/conversations")
def individuals_conversations(limit: int = 500, customer_id: Optional[str] = None,
                              days: Optional[int] = None, from_date: Optional[str] = None,
                              to_date: Optional[str] = None):
    """Лента диалогов физиков (последние первые)."""
    db = SessionLocal()
    try:
        q = db.query(DBIndividual)
        if customer_id:
            q = q.filter(DBIndividual.customer_id == customer_id)
        if from_date and to_date:
            try:
                fd = datetime.fromisoformat(from_date); td = datetime.fromisoformat(to_date)
                q = q.filter(DBIndividual.created_at >= datetime(fd.year, fd.month, fd.day),
                             DBIndividual.created_at < datetime(td.year, td.month, td.day) + timedelta(days=1))
            except ValueError:
                pass
        elif days:
            q = q.filter(DBIndividual.created_at >= datetime.utcnow() - timedelta(days=days))
        rows = q.order_by(desc(DBIndividual.created_at)).limit(limit).all()
        return [{
            "id": c.id, "type": c.type, "customer_id": c.customer_id,
            "tags": c.tags, "record_url": c.record_url,
            "products": _ind_products(c.products),
            "turns": len(c.transcript or []),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "len_sec": c.len_sec,
        } for c in rows]
    finally:
        db.close()


@app.get("/individuals/conversations/{dlg_id}")
def individuals_conversation(dlg_id: str):
    """Полный диалог физика (ES+EN, ссылка, продукты) + история этого клиента.
    Переводит лениво, если ещё не переведён."""
    db = SessionLocal()
    try:
        c = db.query(DBIndividual).filter_by(id=dlg_id).first()
        if not c:
            return {"error": "not found"}
        # перевод НЕ блокирует открытие: EN проставляет фоновый бэкфилл
        # (/admin/individuals/translate). Не переведённые реплики UI покажет как ES.
        history = []
        if c.customer_id:
            others = (db.query(DBIndividual)
                        .filter(DBIndividual.customer_id == c.customer_id, DBIndividual.id != c.id)
                        .order_by(desc(DBIndividual.created_at)).all())
            history = [{"id": o.id, "type": o.type, "tags": o.tags,
                        "created_at": o.created_at.isoformat() if o.created_at else None}
                       for o in others]
        return {
            "id": c.id, "type": c.type, "customer_id": c.customer_id,
            "tags": c.tags, "record_url": c.record_url,
            "products": _ind_products(c.products),
            "transcript": c.transcript or [],
            "len_sec": c.len_sec, "n_tasks": c.n_tasks,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "customer_history": history,
        }
    finally:
        db.close()


@app.get("/individuals/customers")
def individuals_customers(limit: int = 300):
    """Список клиентов-физиков с агрегатами (по customer_id)."""
    db = SessionLocal()
    try:
        rows = (db.query(DBIndividual).filter(DBIndividual.customer_id.isnot(None)).all())
        from collections import defaultdict
        agg = defaultdict(lambda: {"count": 0, "first": None, "last": None, "products": set(), "tags": defaultdict(int)})
        for c in rows:
            a = agg[c.customer_id]
            a["count"] += 1
            if c.created_at:
                a["first"] = min(a["first"], c.created_at) if a["first"] else c.created_at
                a["last"] = max(a["last"], c.created_at) if a["last"] else c.created_at
            for p in _ind_products(c.products):
                a["products"].add(p)
            for t in (c.tags or "").split("|"):
                t = t.strip()
                if t:
                    a["tags"][t] += 1
        out = []
        for cid, a in agg.items():
            out.append({
                "customer_id": cid, "count": a["count"],
                "first_at": a["first"].isoformat() if a["first"] else None,
                "last_at": a["last"].isoformat() if a["last"] else None,
                "products": sorted(a["products"]),
                "top_tags": [t for t, _ in sorted(a["tags"].items(), key=lambda x: -x[1])[:6]],
            })
        out.sort(key=lambda x: -x["count"])
        return out[:limit]
    finally:
        db.close()


@app.get("/individuals/customers/{customer_id}")
def individuals_customer(customer_id: str):
    """Профиль клиента-физика: все его диалоги с транскриптами (для инлайн-просмотра)."""
    db = SessionLocal()
    try:
        rows = (db.query(DBIndividual).filter(DBIndividual.customer_id == customer_id)
                  .order_by(desc(DBIndividual.created_at)).all())
        # перевод НЕ блокирует открытие профиля — его делает фоновый бэкфилл
        convs = [{
            "id": c.id, "type": c.type, "tags": c.tags, "record_url": c.record_url,
            "products": _ind_products(c.products), "turns": len(c.transcript or []),
            "transcript": c.transcript or [],
            "created_at": c.created_at.isoformat() if c.created_at else None,
        } for c in rows]
        products = sorted({p for c in rows for p in _ind_products(c.products)})
        return {
            "customer_id": customer_id, "count": len(rows),
            "first_at": convs[-1]["created_at"] if convs else None,
            "last_at": convs[0]["created_at"] if convs else None,
            "products": products,
            "conversations": convs,
        }
    finally:
        db.close()


@app.get("/privacy")
def privacy_policy():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ScoreOPS Inspector — Privacy Policy</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 720px; margin: 60px auto;
           padding: 0 24px; color: #1e1e2e; line-height: 1.7; }
    h1 { color: #6d28d9; }
    h2 { color: #374151; margin-top: 32px; }
    p, li { color: #4b5563; }
    footer { margin-top: 48px; color: #9ca3af; font-size: 13px; }
  </style>
</head>
<body>
  <h1>ScoreOPS Inspector — Privacy Policy</h1>
  <p><strong>Last updated: May 1, 2026</strong></p>

  <h2>What this extension does</h2>
  <p>ScoreOPS Inspector is an internal quality assurance tool for Plata Bank support teams.
     It automates customer-simulation conversations in the Plata Bank support chat widget
     and evaluates support agent responses.</p>

  <h2>Data collected</h2>
  <ul>
    <li>Text content of support chat messages on <strong>bancoplata.mx</strong></li>
    <li>Agent response times and names (as displayed in the chat widget)</li>
    <li>AI-generated evaluation scores and explanations</li>
  </ul>
  <p>No personal data of real customers is collected. The simulated customer personas
     are entirely synthetic (AI-generated fictional profiles).</p>

  <h2>How data is used</h2>
  <p>Collected data is sent exclusively to the ScoreOPS backend server
     (<code>web-production-48192.up.railway.app</code>) and stored in a private
     PostgreSQL database. Data is used solely for internal QA reporting and
     agent performance improvement within Plata Bank.</p>

  <h2>Data sharing</h2>
  <p>Data is <strong>not sold, shared, or disclosed</strong> to any third party.
     It is accessible only to authorized Plata Bank QA personnel.</p>

  <h2>Data retention</h2>
  <p>Session logs are retained for QA analysis purposes and may be deleted upon request.</p>

  <h2>Contact</h2>
  <p>For questions about this policy, contact the ScoreOPS administrator.</p>

  <footer>ScoreOPS Inspector · Internal QA Tool · Plata Bank PFAE</footer>
</body>
</html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
