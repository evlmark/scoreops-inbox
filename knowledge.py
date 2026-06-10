import os
import warnings
warnings.filterwarnings("ignore")
from pypdf import PdfReader

KNOWLEDGE_BASE_DIR = os.path.join(os.path.dirname(__file__), "Learning Base")


def load_from_db() -> str:
    """Собирает базу знаний из активных документов таблицы documents.
    Возвращает '' если таблицы/данных нет — тогда зовущий упадёт на PDF."""
    try:
        from db import SessionLocal, Document
    except Exception:
        return ""
    session = SessionLocal()
    try:
        q = session.query(Document).filter(Document.removed_at.is_(None))
        # Исключаем внутренние агентские инструкции (если колонка internal уже есть)
        try:
            q = q.filter((Document.internal == 0) | (Document.internal.is_(None)))
        except Exception:
            pass
        docs = q.order_by(Document.slug).all()
        if not docs:
            return ""
        parts = []
        for d in docs:
            title = d.title or d.slug or d.url
            parts.append(f"=== {title} ===\n{d.markdown}")
        full = "\n\n".join(parts)
        print(f"База знаний из БД: {len(docs)} документов, {len(full):,} символов")
        return full
    except Exception as e:
        print(f"  load_from_db: ошибка чтения documents ({e}) — фолбэк на PDF")
        return ""
    finally:
        session.close()


def load_from_pdf() -> str:
    """Читает все PDF из Learning Base и возвращает единый текст (фолбэк)."""
    texts = []
    pdf_files = [f for f in os.listdir(KNOWLEDGE_BASE_DIR) if f.endswith(".pdf")]

    if not pdf_files:
        raise FileNotFoundError(f"PDF файлы не найдены в {KNOWLEDGE_BASE_DIR}")

    for filename in sorted(pdf_files):
        path = os.path.join(KNOWLEDGE_BASE_DIR, filename)
        reader = PdfReader(path)
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text.strip())
        doc_text = "\n".join(pages_text)
        texts.append(f"=== {filename} ===\n{doc_text}")
        print(f"  Загружен: {filename} ({len(reader.pages)} стр.)")

    full_text = "\n\n".join(texts)
    print(f"\nБаза знаний (PDF): {len(pdf_files)} файлов, {len(full_text):,} символов")
    return full_text


def load_knowledge_base() -> str:
    """Источник базы знаний: сначала таблица documents (спарсенный сайт),
    при пустой/недоступной БД — PDF из Learning Base."""
    db_text = load_from_db()
    if db_text:
        return db_text
    return load_from_pdf()


if __name__ == "__main__":
    kb = load_knowledge_base()
    print(kb[:500])
