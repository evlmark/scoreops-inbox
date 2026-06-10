"""
Прод-вход ночного парсера Google Sites.

- Куку берёт из переменной окружения GSITES_COOKIES (JSON),
  локально — фолбэк на файл (GSITES_COOKIES_FILE или scraper/_cookies.json).
- Headless-обход сайта (BFS) через scraper.engine.
- Дифф added/updated/removed -> таблица documents.
- Лог запуска -> таблица crawl_runs.
- Защита от ложного обнуления: если страниц собрано подозрительно мало
  (кука протухла / сайт лёг) — removed не применяем, ран помечаем failed/partial.

Запуск:  python3 crawl.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime

import httpx
from playwright.async_api import async_playwright

from scraper.engine import (
    START_URL, BASE_PREFIX, extract_page, slugify,
)
from db import SessionLocal, init_db, CrawlRun, Document
from kb_import import apply_documents, filter_records

# Если активных страниц в базе уже N, а свежий краул собрал меньше этой доли —
# считаем результат ненадёжным и НЕ трогаем removed.
MIN_RATIO = 0.7
# Абсолютный минимум на случай первого запуска по пустой базе.
MIN_PAGES_ABS = 5

RELOAD_URL = os.getenv("KB_RELOAD_URL")          # например https://<api>/admin/reload-knowledge
EXTENSION_API_KEY = os.getenv("EXTENSION_API_KEY")


def load_cookies():
    raw = os.getenv("GSITES_COOKIES")
    if raw:
        return json.loads(raw)
    path = os.getenv("GSITES_COOKIES_FILE", os.path.join("scraper", "_cookies.json"))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    raise RuntimeError("Нет куки: задайте GSITES_COOKIES или GSITES_COOKIES_FILE")


async def crawl():
    cookies = load_cookies()
    results = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(storage_state={"cookies": cookies})
        page = await ctx.new_page()
        visited, queue = set(), [START_URL.rstrip("/")]
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            print(f"({len(visited)}) {url}", flush=True)
            try:
                data, links = await extract_page(page, url)
                if data and data["markdown"]:
                    results.append({
                        "url": data["url"],
                        "slug": slugify(url),
                        "title": data["title"],
                        "markdown": data["markdown"],
                    })
                    print(f"      ✅ {len(data['markdown'])} символов", flush=True)
                for link in sorted(links):
                    if link not in visited and link not in queue:
                        queue.append(link)
            except Exception as e:
                print(f"      ❌ {e}", flush=True)
        await b.close()
    return results


def maybe_reload_web():
    """Просит web-сервис перечитать базу знаний после успешного краула."""
    if not RELOAD_URL:
        return
    try:
        headers = {"X-Extension-Key": EXTENSION_API_KEY} if EXTENSION_API_KEY else {}
        httpx.post(RELOAD_URL, headers=headers, timeout=30.0)
        print("🔄 web попросили перечитать базу", flush=True)
    except Exception as e:
        print(f"⚠️  reload web не удался: {e}", flush=True)


def main():
    init_db()
    session = SessionLocal()
    run = CrawlRun(started_at=datetime.utcnow(), status="running")
    session.add(run)
    session.commit()

    try:
        results = asyncio.run(crawl())
        kept = filter_records(results)
        active_now = session.query(Document).filter(Document.removed_at.is_(None)).count()

        # Решаем, надёжен ли результат для применения removed.
        threshold = max(MIN_PAGES_ABS, int(active_now * MIN_RATIO))
        reliable = len(kept) >= threshold

        result = apply_documents(session, results, apply_removals=reliable)

        run.finished_at = datetime.utcnow()
        run.pages_total = len(kept)
        run.chars_total = sum(len(r["markdown"]) for r in kept)
        run.tables_total = sum(r["markdown"].count("\n| --- ") for r in kept)
        run.pages_added = result["added"]
        run.pages_updated = result["updated"]
        run.pages_removed = result["removed"]
        run.log = result["changes"]

        if not reliable:
            run.status = "partial"
            run.error_text = (
                f"Собрано {len(kept)} стр. < порога {threshold} "
                f"(активных в базе {active_now}). removed не применялись."
            )
            print(f"⚠️  {run.error_text}", flush=True)
        else:
            run.status = "success"
        session.commit()
        print(f"✅ {run.status}: +{result['added']} ~{result['updated']} -{result['removed']}", flush=True)

        if run.status == "success":
            maybe_reload_web()
    except Exception as e:
        session.rollback()
        run.status = "failed"
        run.finished_at = datetime.utcnow()
        run.error_text = str(e)
        session.commit()
        print(f"❌ Краул упал: {e}", flush=True)
        session.close()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
