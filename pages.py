"""
Server-side pages store: lets analysts publish static reports (HTML, images) into the
dashboard without a git push / redeploy.

  POST /admin/pages/{name}      body = raw bytes, Content-Type = the type to serve
                                (auth: X-Extension-Key, like every /admin/* route)
  GET  /dashboard-pages/{name}  serves it to signed-in users only. AuthMiddleware treats
                                every "/dashboard*" path as public (the dashboard shell has
                                to load before sign-in), so the check lives here: a valid
                                Google ID token in the scoreops_id_token cookie (the
                                dashboard sets it on sign-in, path /dashboard-pages) or
                                X-Extension-Key. Anyone else is redirected to the
                                dashboard sign-in and comes back to the page after it.
  GET  /admin/pages             list stored pages

Pages live in Postgres (table static_pages), so they survive redeploys and need no volume.
Relative links inside a page (e.g. <img src="img_x.jpg">) resolve to /dashboard-pages/img_x.jpg,
so upload the images under the same names.
"""
from datetime import datetime

from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from db import engine

try:
    from auth import verify_google_token, EXTENSION_API_KEY
    _AUTH = True
except Exception:  # same fallback as server.py: auth broken -> do not lock everyone out
    _AUTH = False

TOKEN_COOKIE = "scoreops_id_token"


def _signed_in(request: Request) -> bool:
    if not _AUTH:
        return True
    if EXTENSION_API_KEY and request.headers.get("X-Extension-Key") == EXTENSION_API_KEY:
        return True
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        return False
    try:
        verify_google_token(token)
        return True
    except Exception:
        return False

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


@router.delete("/admin/pages/{name}")
def delete_page(name: str):
    """Убрать страницу из хранилища. Нужно, чтобы опубликованная по ошибке или
    переехавшая в другое место страница не висела мёртвой ссылкой в дашборде."""
    _ensure_table()
    with engine.begin() as conn:
        res = conn.execute(text("delete from static_pages where name = :n"), {"n": name})
    return {"ok": True, "name": name, "deleted": res.rowcount or 0}


@router.get("/admin/pages")
def list_pages():
    _ensure_table()
    with engine.begin() as conn:
        rows = conn.execute(text(
            "select name, content_type, size_bytes, updated_at from static_pages order by name")).fetchall()
    return {"pages": [{"name": r[0], "content_type": r[1], "size_bytes": r[2],
                       "updated_at": r[3].isoformat() if r[3] else None} for r in rows]}


@router.get("/dashboard-pages/{name}")
def get_page(name: str, request: Request):
    if not _signed_in(request):
        if request.headers.get("sec-fetch-dest") == "iframe":
            # inside a dashboard tab: a redirect would load the whole dashboard into the frame
            return Response(status_code=401, media_type="text/html", content=(
                '<p style="font:14px sans-serif;padding:16px">Your session expired. '
                '<a href="/dashboard/" target="_top">Sign in again</a> to see this report.</p>'))
        return RedirectResponse("/dashboard/?next=" + quote(f"/dashboard-pages/{name}", safe=""), status_code=302)
    _ensure_table()
    with engine.begin() as conn:
        row = conn.execute(text(
            "select content_type, content, updated_at from static_pages where name = :n"), {"n": name}).fetchone()
    if not row:
        return Response(status_code=404, content="not found")
    headers = {"Cache-Control": "private, no-cache"}
    if row[2]:
        headers["Last-Modified"] = row[2].strftime("%a, %d %b %Y %H:%M:%S GMT")
    return Response(content=bytes(row[1]), media_type=row[0], headers=headers)
