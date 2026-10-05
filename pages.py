"""
Server-side pages store: lets analysts publish static reports (HTML, images) into the
dashboard without a git push / redeploy.

  POST /admin/pages/{name}      body = raw bytes, Content-Type = the type to serve
                                (auth: X-Extension-Key, like every /admin/* route)
  GET  /dashboard-pages/{name}  serves it (public, same as /dashboard/* static files:
                                the path starts with "/dashboard" so AuthMiddleware
                                treats it as public, and it is NOT under the
                                StaticFiles mount at /dashboard/, so it reaches us)
  GET  /admin/pages             list stored pages

Pages live in Postgres (table static_pages), so they survive redeploys and need no volume.
Relative links inside a page (e.g. <img src="img_x.jpg">) resolve to /dashboard-pages/img_x.jpg,
so upload the images under the same names.
"""
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from db import engine

router = APIRouter()
MAX_BYTES = 25 * 1024 * 1024
_READY = {"ok": False}


def _ensure_table():
    if _READY["ok"]:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "create table if not exists static_pages ("
            " name varchar(200) primary key,"
            " content_type varchar(100) not null,"
            " content bytea not null,"
            " size_bytes integer not null,"
            " updated_at timestamp not null)"
        ))
    _READY["ok"] = True


@router.post("/admin/pages/{name}")
async def put_page(name: str, request: Request):
    body = await request.body()
    if not body:
        return {"ok": False, "error": "empty body"}
    if len(body) > MAX_BYTES:
        return {"ok": False, "error": f"too large: {len(body)} > {MAX_BYTES}"}
    ctype = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    _ensure_table()
    with engine.begin() as conn:
        conn.execute(text(
            "insert into static_pages (name, content_type, content, size_bytes, updated_at)"
            " values (:n, :t, :c, :s, :u)"
            " on conflict (name) do update set content_type = excluded.content_type,"
            " content = excluded.content, size_bytes = excluded.size_bytes, updated_at = excluded.updated_at"
        ), {"n": name, "t": ctype, "c": body, "s": len(body), "u": datetime.utcnow()})
    return {"ok": True, "name": name, "content_type": ctype, "size_bytes": len(body),
            "url": f"/dashboard-pages/{name}"}


@router.get("/admin/pages")
def list_pages():
    _ensure_table()
    with engine.begin() as conn:
        rows = conn.execute(text(
            "select name, content_type, size_bytes, updated_at from static_pages order by name")).fetchall()
    return {"pages": [{"name": r[0], "content_type": r[1], "size_bytes": r[2],
                       "updated_at": r[3].isoformat() if r[3] else None} for r in rows]}


@router.get("/dashboard-pages/{name}")
def get_page(name: str):
    _ensure_table()
    with engine.begin() as conn:
        row = conn.execute(text(
            "select content_type, content, updated_at from static_pages where name = :n"), {"n": name}).fetchone()
    if not row:
        return Response(status_code=404, content="not found")
    headers = {"Cache-Control": "no-cache"}
    if row[2]:
        headers["Last-Modified"] = row[2].strftime("%a, %d %b %Y %H:%M:%S GMT")
    return Response(content=bytes(row[1]), media_type=row[0], headers=headers)
