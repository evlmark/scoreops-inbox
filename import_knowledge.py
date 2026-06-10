"""
Одноразовый бэкфилл: заливает knowledge.json (вывод парсера) в таблицу documents.
Запуск:  python3 import_knowledge.py /path/to/knowledge.json
Без аргумента берёт переменную KNOWLEDGE_JSON или дефолтный путь рядом со скрапером.
"""
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse, unquote

from db import SessionLocal, init_db, CrawlRun
from kb_import import apply_documents

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Google-Site-Scrapper", "clean_output", "knowledge.json",
)


def slug_from_url(url: str) -> str:
    path = unquote(urlparse(url).path).rstrip("/")
    seg = path.rsplit("/", 1)[-1] or "index"
    return re.sub(r"[^a-zA-Z0-9]+", "_", seg).strip("_").lower()


def load_records(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    records = []
    for r in data:
        records.append({
            "url": unquote(r["url"]),
            "slug": slug_from_url(r["url"]),
            "title": r.get("title"),
            "markdown": r.get("markdown", ""),
        })
    return records


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("KNOWLEDGE_JSON", DEFAULT_PATH)
    if not os.path.exists(path):
        print(f"❌ Не найден файл: {path}")
        sys.exit(1)

    init_db()
    records = load_records(path)
    chars_total = sum(len(r["markdown"]) for r in records)
    tables_total = sum(r["markdown"].count("\n| --- ") for r in records)

    session = SessionLocal()
    run = CrawlRun(started_at=datetime.utcnow(), status="running")
    session.add(run)
    session.commit()

    try:
        result = apply_documents(session, records, apply_removals=True)
        run.finished_at = datetime.utcnow()
        run.status = "success"
        run.pages_total = len([r for r in records])
        run.chars_total = chars_total
        run.tables_total = tables_total
        run.pages_added = result["added"]
        run.pages_updated = result["updated"]
        run.pages_removed = result["removed"]
        run.log = result["changes"]
        session.commit()
        print(f"✅ Импорт завершён: +{result['added']} ~{result['updated']} "
              f"={result['unchanged']} -{result['removed']} | "
              f"{chars_total:,} символов, {tables_total} таблиц")
    except Exception as e:
        session.rollback()
        run.status = "failed"
        run.finished_at = datetime.utcnow()
        run.error_text = str(e)
        session.commit()
        print(f"❌ Ошибка импорта: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
