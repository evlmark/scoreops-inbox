"""
БД-слой ScoreOPS.
Локально: SQLite (scoreops.db).
На Railway: PostgreSQL через DATABASE_URL.
"""
import os
import json
import re
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, JSON, ForeignKey, text, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///scoreops.db")

# SQLite требует special arg для threading
engine_args = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True)
    chat_id = Column(String, index=True, nullable=True)   # groups 3 scenarios in one operator chat
    chat_part = Column(Integer, nullable=True)             # 1, 2, or 3
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    persona_name = Column(String, index=True)
    persona_job = Column(String)
    persona_city = Column(String)
    persona_income = Column(Integer)
    scenario = Column(String, index=True)

    agent_name = Column(String, index=True, nullable=True)  # детектируется по первым ответам
    avg_score = Column(Float, nullable=True)
    total_messages = Column(Integer, default=0)
    first_response_time_sec = Column(Integer, nullable=True)  # время до первого ответа агента
    bot_history = Column(JSON, nullable=True)      # CustomerBot.history — для восстановления после рестарта
    bot_turn = Column(Integer, nullable=True)       # CustomerBot.turn
    last_customer_msg = Column(Text, nullable=True) # последнее сообщение клиента для оценки

    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, ForeignKey("sessions.id"), index=True)
    turn = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)

    customer = Column(Text)
    agent = Column(Text)
    customer_en = Column(Text, nullable=True)        # перевод реплики клиента на английский
    agent_en = Column(Text, nullable=True)           # перевод реплики агента на английский
    score = Column(Integer, nullable=True)
    critical_error = Column(Integer, default=0)  # bool
    evaluation = Column(JSON, nullable=True)
    response_time_sec = Column(Integer, nullable=True)

    session = relationship("Session", back_populates="messages")


class Call(Base):
    __tablename__ = "calls"
    id = Column(String, primary_key=True)               # internal UUID
    vapi_call_id = Column(String, unique=True, nullable=True, index=True)
    persona_name = Column(String, index=True)
    persona_job = Column(String, nullable=True)
    persona_city = Column(String, nullable=True)
    persona_income = Column(Integer, nullable=True)
    scenario = Column(String, index=True)
    task = Column(Text)                                  # full task text sent to Vapi
    status = Column(String, default="queued")            # queued / in_progress / completed / failed
    transcript = Column(JSON, nullable=True)             # [{role, text, ts}]
    audio_url = Column(String, nullable=True)
    summary = Column(Text, nullable=True)                # call summary / evaluation explanation
    avg_score = Column(Float, nullable=True)             # 1–10
    evaluation = Column(JSON, nullable=True)             # full breakdown from evaluator
    duration_sec = Column(Integer, nullable=True)
    ended_reason = Column(String, nullable=True)         # e.g. silence-timed-out, customer-ended-call
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    """Реальное обращение клиента (импортировано из выгрузки)."""
    __tablename__ = "conversations"
    id = Column(String, primary_key=True)                # TASK_ID из выгрузки
    type = Column(String, index=True)                    # 'chat' / 'call'
    queue_name = Column(String, nullable=True)
    customer_id = Column(String, index=True, nullable=True)
    agent_name = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, index=True, nullable=True)
    in_progress_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    transcript = Column(JSON, nullable=True)             # [{role:'client'/'agent', text, text_en}]
    topic = Column(String, nullable=True)                # короткий топик (EN)
    topic_es = Column(String, nullable=True)             # короткий топик (ES)
    avg_score = Column(Float, nullable=True)             # оценка 1–10
    evaluation = Column(JSON, nullable=True)             # полная разбивка оценщика
    summary = Column(Text, nullable=True)                # краткий итог
    status = Column(String, default="pending", index=True)  # pending / done / failed
    cohort = Column(String, nullable=True, index=True)       # метка временной выборки (напр. 'not_utilized')
    imported_at = Column(DateTime, default=datetime.utcnow)
    # Управляемые топики (таксономия v1.2)
    topic_slug = Column(String, index=True, nullable=True)   # slug из таблицы topics
    topic_source = Column(String, nullable=True)             # 'seed' / 'llm' / 'human'
    topic_confidence = Column(Float, nullable=True)          # уверенность классификатора 0..1
    product_line = Column(String, index=True, nullable=True) # 'PFAE' / 'PM' / 'NA'
    direction = Column(String, index=True, nullable=True)    # 'inbound' / 'outbound'
    account_type = Column(String, index=True, nullable=True) # 'PFAE External'/'PFAE Golden'/'Persona Moral'/'No Empresa account' (из funnel по customer_id)
    tariff = Column(String, nullable=True)                   # имя тарифа из funnel (только при открытом счёте): Emprendedor/Independiente/Empresario…
    merged_into = Column(String, index=True, nullable=True)  # id «основного» диалога, если этот чат — короткий фрагмент-продолжение (склейка)
    questions_extracted = Column(Integer, default=0, index=True)  # извлечены ли вопросы пользователя (для раздела Questions)


class Document(Base):
    """Страница базы знаний, спарсенная с Google Sites."""
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, unique=True, index=True)
    slug = Column(String, index=True)
    title = Column(String, nullable=True)
    markdown = Column(Text)
    content_hash = Column(String, index=True)         # sha256(markdown) — для диффа
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)   # последний краул, где страница была найдена
    updated_at = Column(DateTime, default=datetime.utcnow)  # последнее изменение контента
    removed_at = Column(DateTime, nullable=True, index=True)  # когда страница пропала с сайта
    internal = Column(Integer, default=0)  # bool: внутренняя агентская инструкция (исключать из KB для оценки)


class Topic(Base):
    """Управляемый словарь топиков обращений (таксономия v1.2)."""
    __tablename__ = "topics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String, unique=True, index=True)
    name_en = Column(String, nullable=True)
    name_es = Column(String, nullable=True)
    category = Column(String, index=True, nullable=True)   # ключ/имя категории
    description = Column(Text, nullable=True)              # определение границы (идёт в промпт классификатора)
    status = Column(String, default="active", index=True) # active / archived
    sort_order = Column(Integer, default=0)
    created_by = Column(String, nullable=True)            # 'seed' / email редактора
    created_at = Column(DateTime, default=datetime.utcnow)


class TopicSuggestion(Base):
    """Предложение нового топика от детектора (фаза 4)."""
    __tablename__ = "topic_suggestions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposed_name = Column(String, nullable=True)
    proposed_category = Column(String, nullable=True)
    rationale = Column(Text, nullable=True)
    sample_conv_ids = Column(JSON, nullable=True)         # примеры обращений
    count = Column(Integer, default=0)                    # размер кластера
    period = Column(String, nullable=True)                # окно анализа, напр. 'last 7d'
    status = Column(String, default="pending", index=True)  # pending / accepted / rejected
    created_at = Column(DateTime, default=datetime.utcnow)


class CrawlRun(Base):
    """Лог одного запуска парсера."""
    __tablename__ = "crawl_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", index=True)  # running / success / partial / failed
    pages_total = Column(Integer, default=0)
    chars_total = Column(Integer, default=0)
    tables_total = Column(Integer, default=0)
    pages_added = Column(Integer, default=0)
    pages_updated = Column(Integer, default=0)
    pages_removed = Column(Integer, default=0)
    error_text = Column(Text, nullable=True)
    log = Column(JSON, nullable=True)   # подробности по страницам: [{url, status, chars}]


class PullRun(Base):
    """Лог одной ночной выгрузки чатов из Snowflake в Real Inbox."""
    __tablename__ = "pull_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", index=True)  # success / partial / failed / no_data
    source = Column(String, default="launchd")              # launchd / manual
    date_from = Column(String, nullable=True)               # YYYY-MM-DD (включительно)
    date_to = Column(String, nullable=True)                 # YYYY-MM-DD (исключительно)
    rows_exported = Column(Integer, default=0)              # сколько строк отдал Snowflake
    imported = Column(Integer, default=0)                   # новых обращений создано
    skipped_duplicates = Column(Integer, default=0)
    skipped_empty = Column(Integer, default=0)
    processed = Column(Integer, default=0)                  # оценено по базе знаний
    remaining = Column(Integer, default=0)                  # осталось pending после прогона
    error_text = Column(Text, nullable=True)


class IndividualDialogue(Base):
    """Диалог клиента-физика (Individuals) — отдельная линия от PyME.
    Только отображение: чат ES+EN, ссылка, продуктовые флаги; без топиков/оценки."""
    __tablename__ = "individual_dialogues"
    id = Column(String, primary_key=True)                 # DIALOGUE_ID
    type = Column(String, index=True)                     # 'chat' / 'call' (из CHANNEL)
    customer_id = Column(String, index=True, nullable=True)  # CUSTOMER_ENTITY_ID
    created_at = Column(DateTime, index=True, nullable=True)  # COMM_START_DTTM
    tags = Column(Text, nullable=True)                    # TAGS (pipe-separated)
    transcript = Column(JSON, nullable=True)              # [{role,text,text_en}]
    record_url = Column(String, nullable=True)            # RECORD_URL (у звонков)
    len_sec = Column(Integer, nullable=True)              # DIALOGUE_LEN_SEC
    n_tasks = Column(Integer, nullable=True)
    n_client_msgs = Column(Integer, nullable=True)
    n_agent_msgs = Column(Integer, nullable=True)
    products = Column(JSON, nullable=True)                # {cc,dc,garantizada,plata_plus,inv,pyme,cl}
    status = Column(String, default="pending", index=True)  # pending / translated
    imported_at = Column(DateTime, default=datetime.utcnow)


class QuestionTheme(Base):
    """Тема вопросов пользователей (динамический каталог, формируется LLM под базу знаний)."""
    __tablename__ = "question_themes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Question(Base):
    """Отдельный вопрос клиента, извлечённый из чата (для переписывания базы знаний)."""
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, index=True)   # ссылка на исходный чат (conversations.id)
    customer_id = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, index=True, nullable=True)  # дата исходного чата
    text = Column(Text)                            # формулировка вопроса (ES)
    theme_slug = Column(String, index=True, nullable=True)


class UserAccess(Base):
    """Кто заходит в дашборд (по Google-логину). Обновляется троттлингом из middleware."""
    __tablename__ = "user_access"
    email = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow, index=True)
    hits = Column(Integer, default=0)


def init_db():
    Base.metadata.create_all(engine)
    # Migration: add columns that may not exist in already-created tables
    _run_migrations()
    # Seed: залить словарь топиков, если таблица пуста
    seed_topics()


def _run_migrations():
    """Добавляет новые колонки в существующие таблицы (идемпотентно)."""
    with engine.connect() as conn:
        if DATABASE_URL.startswith("sqlite"):
            # SQLite не поддерживает IF NOT EXISTS для ALTER TABLE — ловим ошибку
            for stmt in [
                "ALTER TABLE sessions ADD COLUMN chat_id VARCHAR",
                "ALTER TABLE sessions ADD COLUMN chat_part INTEGER",
                "ALTER TABLE sessions ADD COLUMN bot_history TEXT",
                "ALTER TABLE sessions ADD COLUMN bot_turn INTEGER",
                "ALTER TABLE sessions ADD COLUMN last_customer_msg TEXT",
                "ALTER TABLE sessions ADD COLUMN first_response_time_sec INTEGER",
                "ALTER TABLE messages ADD COLUMN customer_en TEXT",
                "ALTER TABLE messages ADD COLUMN agent_en TEXT",
                "ALTER TABLE conversations ADD COLUMN cohort VARCHAR",
                "ALTER TABLE conversations ADD COLUMN topic_slug VARCHAR",
                "ALTER TABLE conversations ADD COLUMN topic_source VARCHAR",
                "ALTER TABLE conversations ADD COLUMN topic_confidence FLOAT",
                "ALTER TABLE conversations ADD COLUMN product_line VARCHAR",
                "ALTER TABLE conversations ADD COLUMN direction VARCHAR",
                "ALTER TABLE conversations ADD COLUMN account_type VARCHAR",
                "ALTER TABLE conversations ADD COLUMN tariff VARCHAR",
                "ALTER TABLE conversations ADD COLUMN merged_into VARCHAR",
                "ALTER TABLE conversations ADD COLUMN questions_extracted INTEGER DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN internal INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception:
                    pass  # колонка уже есть
        else:
            # PostgreSQL поддерживает IF NOT EXISTS
            for stmt in [
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chat_id VARCHAR",
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chat_part INTEGER",
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS bot_history JSON",
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS bot_turn INTEGER",
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_customer_msg TEXT",
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS first_response_time_sec INTEGER",
                # calls table columns
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS vapi_call_id VARCHAR",
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS audio_url TEXT",
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS duration_sec INTEGER",
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS ended_reason VARCHAR",
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS evaluation JSON",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS customer_en TEXT",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS agent_en TEXT",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cohort VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_slug VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_source VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_confidence DOUBLE PRECISION",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS product_line VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS direction VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS account_type VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tariff VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS merged_into VARCHAR",
                "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS questions_extracted INTEGER DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS internal INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()


def seed_topics():
    """Заливает словарь топиков из topics_seed.py, если таблица пуста (идемпотентно)."""
    try:
        from topics_seed import TOPICS_SEED
    except Exception as e:
        print(f"[seed_topics] не найден topics_seed: {e}")
        return
    session = SessionLocal()
    try:
        if session.query(Topic).count() > 0:
            return
        for i, t in enumerate(TOPICS_SEED):
            session.add(Topic(
                slug=t["slug"], name_en=t.get("name_en"), name_es=t.get("name_es"),
                category=t.get("category"), description=t.get("description"),
                status="active", sort_order=i, created_by="seed",
            ))
        session.commit()
        print(f"[seed_topics] залито {len(TOPICS_SEED)} топиков")
    except Exception as e:
        session.rollback()
        print(f"[seed_topics] ошибка: {e}")
    finally:
        session.close()


# ===== Парсер имени агента =====

AGENT_PATTERNS = [
    r"Te atiende\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)",
    r"Soy\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\s*[,.]",
    r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\s*·\s*\d{1,2}:\d{2}",
    r"^([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\s*\n",
]

# Имена которые могут спутаться (Plata-бот, общие фразы)
NOT_AGENT_NAMES = {"Plata", "Soporte", "Hola", "Buenos", "Buenas", "Por", "Gracias"}


def detect_agent_name(text: str) -> Optional[str]:
    """Извлекает имя живого агента из его ответа."""
    if not text:
        return None
    for pattern in AGENT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            name = m.group(1)
            if name not in NOT_AGENT_NAMES:
                return name
    return None
