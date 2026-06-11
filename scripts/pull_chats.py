#!/usr/bin/env python3
"""
Ночная выгрузка чатов PyME из Snowflake (через plata-mcp / Superset dwh) в ScoreOPS.

Что делает:
  1. Считает диапазон дат (по умолчанию — вчерашний день в TZ America/Mexico_City).
  2. Поднимает plata-mcp как локальный MCP-сервер (stdio), вызывает Superset.exportSql
     с чат-запросом → получает локальный CSV (использует сохранённую SSO из Keychain).
  3. Заливает CSV в Railway: POST /admin/import-csv (дедуп по TASK_ID уже на бэке).
  4. Гоняет POST /admin/process-conversations пачками до конца (перевод + оценка по базе).
  5. Пишет лог; при сбое — пытается показать macOS-уведомление.

Зависимости: только стандартная библиотека Python 3.9+.
Запуск вручную:
    python3 pull_chats.py                      # вчера
    python3 pull_chats.py --from 2026-05-29 --to 2026-06-01   # [from, to)
    python3 pull_chats.py --date 2026-05-31    # ровно один день
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# ─────────────────────────── конфиг (можно переопределить через env) ───────────────────────────
# URL сервиса scoreops-inbox. launchd ОБЯЗАН задать SCOREOPS_WEB_BASE на боевой URL нового сервиса.
WEB_BASE   = os.getenv("SCOREOPS_WEB_BASE", "https://CHANGE-ME.up.railway.app")
EXT_KEY    = os.getenv("SCOREOPS_EXT_KEY", "scoreops-ext-d8f72b3a1c9e4f5b")
PLATA_BIN  = os.getenv("PLATA_MCP_BIN", os.path.expanduser("~/.local/bin/plata-mcp"))
SUPERSET_ENV = os.getenv("SCOREOPS_SUPERSET_ENV", "dwh")
SUPERSET_DB  = int(os.getenv("SCOREOPS_SUPERSET_DB", "1"))
TZ_NAME    = os.getenv("SCOREOPS_TZ", "America/Mexico_City")

LOG_DIR = os.path.expanduser("~/.plata-mcp/scoreops-pull")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "pull.log")


def log(msg: str):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(title: str, text: str):
    """Best-effort macOS-уведомление (не критично, если не сработает)."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(text)} with title {json.dumps(title)}'],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


# ─────────────────────────── даты ───────────────────────────
def _today_in_tz() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(TZ_NAME)).date()
    except Exception:
        return dt.date.today()


def resolve_range(args) -> tuple[str, str]:
    """Возвращает (date_from, date_to) — обе строки YYYY-MM-DD, интервал [from, to)."""
    if args.date:
        d0 = dt.date.fromisoformat(args.date)
        return d0.isoformat(), (d0 + dt.timedelta(days=1)).isoformat()
    if args.date_from and args.date_to:
        return args.date_from, args.date_to
    # дефолт: скользящее окно последних N дней (DWH заливает день с лагом > суток,
    # поэтому берём с запасом; дубли отсекаются по TASK_ID, поздно прилетевшие дни
    # подхватываются ближайшей ночью).
    lookback = int(os.getenv("SCOREOPS_LOOKBACK_DAYS", "4"))
    today = _today_in_tz()
    start = today - dt.timedelta(days=lookback)
    return start.isoformat(), today.isoformat()


# ─────────────────────────── SQL ───────────────────────────
def build_chats_sql(d_from: str, d_to: str) -> str:
    return f"""with pre as (
    select t1.task_id, t1.queue_name, t1.customerid, t1.created_dttm, t1.in_progress_dttm, t1.closed_dttm
    from dwh_ops_qa_prod.customer_care.t_task_act_extra_data t1
    where (t1.original_queue ilike '%pyme%' or t1.pyme_account_flg = true)
      and t1.created_dttm >= '{d_from}' and t1.created_dttm < '{d_to}'
),
pre_chats_texts as (
   select distinct t1.task_id, t1.send_dttm, t1.sender, t1.text,
          row_number() over (partition by t1.task_id order by t1.send_dttm) as rn
   from dwh_ops_qa_prod.customer_care.t_cs_bot_chats t1
   inner join pre t2 on t1.task_id = t2.task_id
),
chats_texts as (
   select distinct task_id,
          LISTAGG(sender || ' | ' || text, '; ') WITHIN GROUP (order by rn) OVER ( PARTITION BY task_id) as text_final
   from pre_chats_texts
)
select t1.task_id, t1.queue_name, t1.customerid, t1.created_dttm, t1.in_progress_dttm, t1.closed_dttm,
       chat.text_final as chat_text
from pre t1
inner join chats_texts chat on chat.task_id = t1.task_id"""


# ─────────────────────────── MCP stdio клиент ───────────────────────────
class MCP:
    def __init__(self, binary: str):
        self.p = subprocess.Popen(
            [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._id = 0

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _read_id(self, want_id, timeout):
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                time.sleep(0.05)
                if self.p.poll() is not None:
                    raise RuntimeError("plata-mcp завершился преждевременно")
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == want_id:
                return msg
        raise TimeoutError(f"нет ответа на id={want_id} за {timeout}s")

    def initialize(self, timeout=60):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "scoreops-pull", "version": "1.0"}}})
        res = self._read_id(self._id, timeout)
        if "result" not in res:
            raise RuntimeError(f"initialize failed: {json.dumps(res)[:300]}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def code_execute(self, script: str, timeout_seconds: int = 110):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": "code_execute",
                               "arguments": {"script": script, "timeout_seconds": str(timeout_seconds)}}})
        res = self._read_id(self._id, timeout=timeout_seconds + 25)
        if "result" not in res:
            raise RuntimeError(f"code_execute error: {json.dumps(res)[:300]}")
        content = res["result"].get("content") or []
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return text

    def close(self):
        try:
            self.p.terminate()
        except Exception:
            pass


def export_chats_csv(mcp: MCP, sql: str) -> dict:
    """Вызывает Superset.exportSql через code_execute, возвращает dict манифеста экспорта."""
    js = (
        "var r = await Superset.exportSql({env:%r, database_id:%d, sql:%s, format:'csv', order_by:'task_id'});"
        "console.log(typeof r==='string'?r:JSON.stringify(r));"
        % (SUPERSET_ENV, SUPERSET_DB, json.dumps(sql))
    )
    raw = mcp.code_execute(js, timeout_seconds=110)
    # raw — это JSON вида {"output":"<console>\n","result":...}
    try:
        outer = json.loads(raw)
        out = outer.get("output", raw)
    except Exception:
        out = raw
    out = (out or "").strip()
    try:
        manifest = json.loads(out)
    except Exception:
        raise RuntimeError(f"не удалось разобрать ответ экспорта: {out[:300]}")
    if isinstance(manifest, str):  # tool вернул строку ошибки
        raise RuntimeError(f"экспорт не удался: {manifest[:300]}")
    if manifest.get("status") != "completed":
        raise RuntimeError(f"экспорт статус={manifest.get('status')}: {out[:300]}")
    return manifest


# ─────────────────── Funnel: тип Empresa-аккаунта по customer_id ───────────────────
_BAD_IDS = {"", "<nil>", "nil", "null", "none", "n/a", "na", "-"}


def csv_customer_ids(path: str):
    """Уникальные непустые CUSTOMERID из CSV."""
    import csv as _csv
    ids = set()
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in _csv.DictReader(f):
            v = (row.get("CUSTOMERID") or "").strip()
            if v and v.lower() not in _BAD_IDS:
                ids.add(v)
    return sorted(ids)


def _table_rows(text: str):
    """ASCII-таблица Snowflake.query (строка) → список dict по заголовку."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if len(lines) < 2 or "|" not in lines[0]:
        return None  # не таблица (вероятно ошибка)
    header = [h.strip() for h in lines[0].split("|")]
    out = []
    for ln in lines[2:]:
        if ln.lstrip().startswith("(") and ln.rstrip().endswith("rows)"):
            break
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) == len(header):
            out.append(dict(zip(header, parts)))
    return out


def fetch_account_labels(mcp: MCP, customer_ids, chunk: int = 80) -> dict:
    """funnel_pfae по user_id → {customer_id: {'account_type':..., 'tariff':...}}.
    ACCOUNT_CREATED + PFAEGolden→'PFAE Golden', + PFAE→'PFAE External', иначе 'No Empresa account'.
    tariff = имя тарифа открытого счёта (None, если счёта нет). При сбое Snowflake — поднимает исключение."""
    info = {cid: {"account_type": "No Empresa account", "tariff": None} for cid in customer_ids}
    for i in range(0, len(customer_ids), chunk):
        part = customer_ids[i:i + chunk]
        inl = ",".join("'" + c.replace("'", "") + "'" for c in part)
        sql = ("select user_id, "
               "max(case when product_type='PFAEGolden' and current_status='ACCOUNT_CREATED' then 1 else 0 end) golden, "
               "max(case when product_type='PFAE' and current_status='ACCOUNT_CREATED' then 1 else 0 end) ext, "
               "max(case when current_status='ACCOUNT_CREATED' then tariff_name::string end) tariff "
               "from DWH_PYME_MAIN_PROD.ORIGINATION.FUNNEL_PFAE where user_id in (" + inl + ") group by user_id")
        js = ("var r = await Snowflake.query({sql:%s, role:'MARK_EVLAMPIEV'});"
              "console.log(JSON.stringify(r));" % json.dumps(sql))
        raw = mcp.code_execute(js, timeout_seconds=110)
        try:
            out = json.loads(raw).get("output", raw)
        except Exception:
            out = raw
        out = (out or "").strip()
        try:
            obj = json.loads(out)
        except Exception:
            obj = out
        rows = None
        if isinstance(obj, dict) and obj.get("rows") is not None:
            cols = obj.get("columns") or []
            rows = [dict(zip(cols, r)) for r in obj["rows"]]
        elif isinstance(obj, str):
            rows = _table_rows(obj)
        if rows is None:
            raise RuntimeError(f"funnel-запрос не вернул таблицу: {str(out)[:200]}")
        for r in rows:
            cid = r.get("USER_ID")
            if not cid:
                continue
            tar = (r.get("TARIFF") or "").strip().strip('"') or None
            if tar and tar.upper() == "NULL":
                tar = None
            if str(r.get("GOLDEN")) == "1":
                info[cid] = {"account_type": "PFAE Golden", "tariff": tar}
            elif str(r.get("EXT")) == "1":
                info[cid] = {"account_type": "PFAE External", "tariff": tar}
    return info


# ─────────────────────────── HTTP к Railway ───────────────────────────
def http(method: str, path: str, data: bytes = None, ctype: str = None, timeout: int = 120):
    url = WEB_BASE.rstrip("/") + path
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Extension-Key", EXT_KEY)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except Exception:
        return {"_raw": body}


def import_csv_file(path: str) -> dict:
    with open(path, "rb") as f:
        payload = f.read()
    return http("POST", "/admin/import-csv", data=payload, ctype="text/csv; charset=utf-8")


def report_run(summary: dict):
    """Логирует сводку прогона в Railway (/admin/pull-runs). Best-effort."""
    try:
        data = json.dumps(summary).encode("utf-8")
        r = http("POST", "/admin/pull-runs", data=data, ctype="application/json", timeout=30)
        log(f"Сводка записана в лог выгрузок: {json.dumps(r, ensure_ascii=False)}")
    except Exception as e:
        log(f"(не критично) не удалось записать сводку выгрузки: {e}")


def process_all(max_minutes: int = 50) -> dict:
    """Гоняет оценку батчами до конца. Терпит временные сбои (502/таймауты): ретраит
    с паузой, не роняет весь прогон из-за одного плохого ответа."""
    deadline = time.time() + max_minutes * 60
    total = 0
    batches = 0
    errors = 0
    consecutive_errors = 0
    last_remaining = None
    while time.time() < deadline:
        try:
            r = http("POST", "/admin/process-conversations?batch=5", timeout=180)
            consecutive_errors = 0
        except Exception as e:
            errors += 1
            consecutive_errors += 1
            log(f"  батч-ошибка ({consecutive_errors}): {e} — пауза и ретрай")
            if consecutive_errors >= 6:
                return {"processed": total, "batches": batches, "remaining": last_remaining,
                        "errors": errors, "done": False}
            time.sleep(min(60, 10 * consecutive_errors))
            continue
        total += int(r.get("processed_this_batch", 0) or 0)
        last_remaining = r.get("remaining_pending", 0)
        batches += 1
        if r.get("done") or (last_remaining == 0 and r.get("processed_this_batch", 0) == 0):
            return {"processed": total, "batches": batches, "remaining": last_remaining,
                    "errors": errors, "done": True}
    return {"processed": total, "batches": batches, "remaining": last_remaining or "timeout",
            "errors": errors, "done": False}


# ─────────────────────────── main ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="один день YYYY-MM-DD")
    ap.add_argument("--from", dest="date_from", help="начало диапазона YYYY-MM-DD (включительно)")
    ap.add_argument("--to", dest="date_to", help="конец диапазона YYYY-MM-DD (исключительно)")
    ap.add_argument("--csv", help="импортировать готовый CSV-файл вместо запроса в Snowflake")
    ap.add_argument("--no-process", action="store_true", help="только импорт, без оценки")
    args = ap.parse_args()

    d_from, d_to = resolve_range(args)
    started = dt.datetime.now().replace(microsecond=0).isoformat()
    log(f"=== PULL START  range=[{d_from}, {d_to})  web={WEB_BASE} ===")

    source = "launchd" if "scoreops" in os.getenv("XPC_SERVICE_NAME", "").lower() else "manual"
    summary = {
        "started_at": started, "date_from": d_from, "date_to": d_to, "source": source,
        "status": "failed", "rows_exported": 0, "imported": 0,
        "skipped_duplicates": 0, "skipped_empty": 0, "processed": 0, "remaining": 0,
        "error_text": None,
    }

    def finish(code: int):
        summary["finished_at"] = dt.datetime.now().replace(microsecond=0).isoformat()
        report_run(summary)
        log("=== PULL DONE ===")
        return code

    # 1) Источник CSV: готовый файл (--csv) или экспорт из Snowflake
    if args.csv:
        csv_path = os.path.expanduser(args.csv)
        if not os.path.exists(csv_path):
            summary["error_text"] = f"csv not found: {csv_path}"
            log(f"ОШИБКА: файл не найден: {csv_path}")
            return finish(2)
        log(f"Источник — готовый CSV: {csv_path}")
    else:
        mcp = None
        try:
            mcp = MCP(PLATA_BIN)
            mcp.initialize()
            log("MCP инициализирован, запускаю экспорт из Snowflake…")
            manifest = export_chats_csv(mcp, build_chats_sql(d_from, d_to))
            files = manifest.get("files") or []
            rows = int(manifest.get("row_count", 0) or 0)
            summary["rows_exported"] = rows
            log(f"Экспорт ок: {rows} строк, файл: {files[0] if files else '—'}")
            if not files or rows == 0:
                summary["status"] = "no_data"
                log("Чатов за период нет — выходим без импорта.")
                return finish(0)
            csv_path = files[0]
        except Exception as e:
            summary["error_text"] = f"export: {e}"
            log(f"ОШИБКА экспорта из Snowflake: {e}")
            notify("ScoreOPS: выгрузка чатов не удалась",
                   f"Snowflake/plata-mcp: {e}. Возможно, истекла SSO — запусти 'plata-mcp login'.")
            return finish(2)
        finally:
            if mcp:
                mcp.close()

    # 2) Импорт в Railway
    try:
        res = import_csv_file(csv_path)
        summary["imported"] = int(res.get("imported", 0) or 0)
        summary["skipped_duplicates"] = int(res.get("skipped_duplicates", 0) or 0)
        summary["skipped_empty"] = int(res.get("skipped_empty_text", 0) or 0)
        if args.csv:  # для готового CSV «выгружено» = всё, что прочитал импортёр
            summary["rows_exported"] = summary["imported"] + summary["skipped_duplicates"] + summary["skipped_empty"]
        log(f"Импорт в Railway: {json.dumps(res, ensure_ascii=False)}")
    except Exception as e:
        summary["error_text"] = f"import: {e}"
        log(f"ОШИБКА импорта в Railway: {e}")
        notify("ScoreOPS: импорт чатов не удался", str(e))
        return finish(3)

    # 2.5) Обогащение типом Empresa-аккаунта по funnel (best-effort; требует Snowflake/SSO)
    try:
        cids = csv_customer_ids(csv_path)
        if cids:
            log(f"Funnel: тяну account_type для {len(cids)} пользователей…")
            m2 = MCP(PLATA_BIN)
            m2.initialize()
            try:
                info = fetch_account_labels(m2, cids)
            finally:
                m2.close()
            accounts = [{"customer_id": c, "account_type": v["account_type"], "tariff": v.get("tariff")}
                        for c, v in info.items()]
            r = http("POST", "/admin/user-accounts",
                     data=json.dumps({"accounts": accounts}).encode("utf-8"),
                     ctype="application/json", timeout=120)
            summary["accounts_updated"] = r.get("users")
            log(f"Funnel: account_type обновлён — {json.dumps(r, ensure_ascii=False)}")
    except Exception as e:
        log(f"Funnel: пропускаю обогащение account_type ({e})")
        notify("ScoreOPS: funnel-обогащение account_type не удалось",
               f"{e}. Возможно, истекла Snowflake-SSO (plata-mcp login) или нет доступа к funnel.")

    if args.no_process:
        summary["status"] = "success"
        log("--no-process: пропускаю оценку.")
        return finish(0)

    # 3) Обработка (перевод + оценка)
    try:
        log("Запускаю обработку (перевод + оценка по базе знаний)…")
        proc = process_all()
        summary["processed"] = int(proc.get("processed", 0) or 0)
        rem = proc.get("remaining", 0)
        summary["remaining"] = int(rem) if isinstance(rem, int) else 0
        summary["status"] = "success" if proc.get("done") else "partial"
        log(f"Обработка: {json.dumps(proc, ensure_ascii=False)}")
    except Exception as e:
        summary["status"] = "partial"
        summary["error_text"] = f"process: {e}"
        log(f"ОШИБКА обработки: {e} (чаты импортированы, можно дообработать вручную)")
        notify("ScoreOPS: оценка чатов не завершилась", str(e))
        return finish(4)

    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
