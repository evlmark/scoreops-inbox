"""
Заливка спарсенных страниц в таблицу documents с диффом.
Используется и одноразовым бэкфиллом, и ночным краулом.
"""
import hashlib
from datetime import datetime

from db import Document

# Лендинги-меню без полезного контента — не кладём в базу знаний.
SKIP_SLUGS = {"pyme", "pyme-pm-pfae"}
MIN_CHARS = 200  # короче — считаем пустым меню, пропускаем


def _hash(markdown: str) -> str:
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()


def filter_records(records):
    """Отбрасывает пустые лендинги."""
    out = []
    for r in records:
        slug = (r.get("slug") or "").strip()
        md = r.get("markdown") or ""
        if slug in SKIP_SLUGS:
            continue
        if len(md.strip()) < MIN_CHARS:
            continue
        out.append(r)
    return out


def apply_documents(session, records, apply_removals=True):
    """
    Сверяет records ({url, slug, title, markdown}) с таблицей documents.
    Возвращает dict со счётчиками и списком изменений.

    apply_removals=False — не помечать пропавшие страницы removed
    (защита от ложного обнуления, когда краул собрал мало).
    """
    now = datetime.utcnow()
    records = filter_records(records)
    seen_urls = set()

    added = updated = unchanged = removed = 0
    changes = []

    existing = {d.url: d for d in session.query(Document).all()}

    for r in records:
        url = r["url"]
        seen_urls.add(url)
        h = _hash(r.get("markdown", ""))
        doc = existing.get(url)

        if doc is None:
            doc = Document(
                url=url,
                slug=r.get("slug"),
                title=r.get("title"),
                markdown=r.get("markdown", ""),
                content_hash=h,
                first_seen=now,
                last_seen=now,
                updated_at=now,
                removed_at=None,
            )
            session.add(doc)
            added += 1
            changes.append({"url": url, "change": "added"})
        else:
            doc.last_seen = now
            doc.slug = r.get("slug")
            doc.title = r.get("title")
            was_removed = doc.removed_at is not None
            if doc.content_hash != h or was_removed:
                doc.markdown = r.get("markdown", "")
                doc.content_hash = h
                doc.updated_at = now
                doc.removed_at = None
                updated += 1
                changes.append({"url": url, "change": "restored" if was_removed else "updated"})
            else:
                unchanged += 1

    # Пропавшие страницы: были активны, но в этом крауле их нет.
    if apply_removals:
        for url, doc in existing.items():
            if url not in seen_urls and doc.removed_at is None:
                doc.removed_at = now
                removed += 1
                changes.append({"url": url, "change": "removed"})

    return {
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "changes": changes,
    }
