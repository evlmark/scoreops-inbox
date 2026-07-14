#!/usr/bin/env python3
"""Разовая загрузка диалогов физиков (Individuals) из CSV в таблицу individual_dialogues.
Локальный скрипт: DBURL=<prod public url> CSV=<path> python3 analysis/import_individuals.py
Парсит DIALOGUE_TEXT (строки 'ММ-ДД ЧЧ:ММ client/agent: текст') в transcript [{role,text}]."""
import os, csv, re, sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", os.environ.get("DBURL", ""))
csv.field_size_limit(10 ** 7)

from db import SessionLocal, IndividualDialogue, Base, engine  # noqa

LINE_RE = re.compile(r"^(\d{2}-\d{2} \d{2}:\d{2}) (client|agent|bot): (.*)$")

_ROLE = {"client": "customer", "bot": "bot", "agent": "agent"}


# формат звонков: "Agent | 5.6-9.8 | текст; Client | 10.4-26.7 | текст; ..."
# (роль | тайминг | текст, сегменты через '; '; всё может быть в одну строку)
_CALL_ROLE = {"client": "customer", "agent": "agent", "bot": "bot"}
_CALL_SPLIT = re.compile(r"\s*;\s*(?=(?:Agent|Client|Bot)\s*\|\s*[\d.\-]+\s*\|)", re.I)
_CALL_SEG = re.compile(r"^(Agent|Client|Bot)\s*\|\s*[\d.\-]+\s*\|\s*(.*)$", re.I | re.S)


def _looks_like_call(text: str) -> bool:
    return bool(re.match(r"^\s*(Agent|Client|Bot)\s*\|\s*[\d.\-]+\s*\|", text or "", re.I))


def parse_call_transcript(text: str):
    turns = []
    for part in _CALL_SPLIT.split(text or ""):
        m = _CALL_SEG.match(part.strip())
        if m:
            role = _CALL_ROLE.get(m.group(1).lower(), "agent")
            t = (m.group(2) or "").strip()
            if t:
                turns.append({"role": role, "text": t})
    return turns


def parse_transcript(text: str):
    if _looks_like_call(text):            # звонок — другой формат
        return parse_call_transcript(text)
    turns = []
    for raw in (text or "").split("\n"):
        m = LINE_RE.match(raw)
        if m:
            role = _ROLE.get(m.group(2), "agent")
            turns.append({"role": role, "text": m.group(3)})
        elif turns:                       # продолжение предыдущей реплики
            turns[-1]["text"] += "\n" + raw
    return [t for t in turns if (t.get("text") or "").strip()]


def flag(v):
    return True if str(v).strip().lower() == "true" else (False if str(v).strip().lower() == "false" else None)


def main():
    csv_path = os.environ["CSV"]
    Base.metadata.create_all(engine)   # создаст таблицу, если её ещё нет
    db = SessionLocal()
    n = 0
    try:
        rows = list(csv.DictReader(open(csv_path)))
        for r in rows:
            did = r["DIALOGUE_ID"]
            try:
                ca = datetime.fromisoformat(r["COMM_START_DTTM"]) if r.get("COMM_START_DTTM") else None
            except Exception:
                ca = None
            def _int(x):
                try: return int(float(x))
                except Exception: return None
            obj = db.query(IndividualDialogue).filter_by(id=did).first() or IndividualDialogue(id=did)
            obj.type = "call" if (r.get("CHANNEL") or "").lower() == "call" else "chat"
            obj.customer_id = (r.get("CUSTOMER_ENTITY_ID") or "").strip() or None
            obj.created_at = ca
            obj.tags = (r.get("TAGS") or "").strip() or None
            obj.transcript = parse_transcript(r.get("DIALOGUE_TEXT"))
            obj.record_url = (r.get("RECORD_URL") or "").strip() or None
            obj.len_sec = _int(r.get("DIALOGUE_LEN_SEC"))
            obj.n_tasks = _int(r.get("N_TASKS"))
            obj.n_client_msgs = _int(r.get("N_CLIENT_MSGS"))
            obj.n_agent_msgs = _int(r.get("N_AGENT_MSGS"))
            obj.products = {
                "cc": flag(r.get("HAS_CC")), "dc": flag(r.get("HAS_DC")),
                "garantizada": flag(r.get("HAS_GARANTIZADA")), "plata_plus": flag(r.get("HAS_PLATA_PLUS")),
                "inv": flag(r.get("HAS_INV")), "pyme": flag(r.get("HAS_PYME")), "cl": flag(r.get("HAS_CL")),
            }
            if obj.status not in ("translated",):
                obj.status = "pending"
            db.add(obj)
            n += 1
            if n % 300 == 0:
                db.commit(); print(f"  …{n}", flush=True)
        db.commit()
        print(f"Загружено/обновлено диалогов: {n}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
