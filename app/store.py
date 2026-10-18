"""SQLite storage: cases (full state as JSON), messages, and the trace of every agent step.

State is saved after every step, so a case interrupted for days (network loss, server
restart) resumes exactly where it stopped.
"""

import json
import sqlite3
import threading
import uuid
from typing import Any

from . import clock
from .config import DB_PATH

_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS cases (
        id TEXT PRIMARY KEY, phone TEXT NOT NULL, state TEXT NOT NULL,
        open INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, updated TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS cases_phone ON cases(phone, open);
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, phone TEXT NOT NULL, channel TEXT NOT NULL,
        direction TEXT NOT NULL, kind TEXT NOT NULL, text TEXT, media_url TEXT, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scenario_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, case_id TEXT, status TEXT NOT NULL,
        started TEXT NOT NULL, finished TEXT, outcome TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, ts TEXT NOT NULL, kind TEXT NOT NULL,
        name TEXT NOT NULL, status TEXT NOT NULL, ms INTEGER, input TEXT, output TEXT);
    """
)


def _now() -> str:
    return clock.now().isoformat(timespec="seconds")


def mask_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"xxxxxx{digits[-4:]}" if len(digits) >= 4 else "xxxx"


def new_case(phone: str, channel: str) -> dict:
    case_id = "C-" + uuid.uuid4().hex[:6].upper()
    state = {
        "id": case_id,
        "phone": phone,
        "channel": channel,
        "language": None,
        "stage": "intake",
        "fields": {},
        "sources": {},
        "conflicts": [],
        "peril_class": None,
        "event_time": None,
        "deadline": None,
        "evidence": [],
        "pending_question": None,
        "awaiting_consent": False,
        "consent_at": None,
        "route_plan": [],
        "attempts": [],
        "intimation": {"status": "not_started", "reference": None, "route": None, "verified_at": None},
        "handoff": None,
        "surveyor": None,
        "escalations": [],
        "next_action": None,
        "action_log": [],
        "history": [],
        "created": _now(),
    }
    with _lock:
        _db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?)", (case_id, phone, json.dumps(state), 1, state["created"], state["created"]))
        _db.commit()
    return state


def insert_case(state: dict, created: str, updated: str) -> None:
    """Used by the demo-data seeder to insert a finished case with its own timestamps."""
    is_open = 0 if state["stage"] in ("closed", "out_of_scope") else 1
    with _lock:
        _db.execute("INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?)", (state["id"], state["phone"], json.dumps(state, ensure_ascii=False), is_open, created, updated))
        _db.commit()


def save_case(state: dict) -> None:
    is_open = 0 if state["stage"] in ("closed", "out_of_scope") else 1
    with _lock:
        _db.execute("UPDATE cases SET state=?, open=?, updated=? WHERE id=?", (json.dumps(state, ensure_ascii=False), is_open, _now(), state["id"]))
        _db.commit()


def get_case(case_id: str) -> dict | None:
    row = _db.execute("SELECT state FROM cases WHERE id=?", (case_id,)).fetchone()
    return json.loads(row["state"]) if row else None


def open_case_for(phone: str) -> dict | None:
    row = _db.execute("SELECT state FROM cases WHERE phone=? AND open=1 ORDER BY created DESC LIMIT 1", (phone,)).fetchone()
    return json.loads(row["state"]) if row else None


def list_cases(open_only: bool = False) -> list[dict]:
    sql = "SELECT state FROM cases" + (" WHERE open=1" if open_only else "") + " ORDER BY updated DESC"
    return [json.loads(r["state"]) for r in _db.execute(sql).fetchall()]


def add_message(case_id: str | None, phone: str, channel: str, direction: str, kind: str, text: str | None, media_url: str | None = None) -> None:
    with _lock:
        _db.execute(
            "INSERT INTO messages (case_id, phone, channel, direction, kind, text, media_url, ts) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, phone, channel, direction, kind, text, media_url, _now()),
        )
        _db.commit()


def messages_for(phone: str, after_id: int = 0) -> list[dict]:
    rows = _db.execute("SELECT * FROM messages WHERE phone=? AND id>? ORDER BY id", (phone, after_id)).fetchall()
    return [dict(r) for r in rows]


def messages_for_case(case_id: str) -> list[dict]:
    return [dict(r) for r in _db.execute("SELECT * FROM messages WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]


def add_trace(case_id: str | None, kind: str, name: str, status: str, ms: int | None, data_in: Any = None, data_out: Any = None) -> None:
    def dump(v: Any) -> str | None:
        if v is None:
            return None
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        return s[:4000]

    with _lock:
        _db.execute(
            "INSERT INTO trace (case_id, ts, kind, name, status, ms, input, output) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, _now(), kind, name, status, ms, dump(data_in), dump(data_out)),
        )
        _db.commit()


def trace_for(case_id: str | None = None, after_id: int = 0, limit: int = 500) -> list[dict]:
    if case_id:
        rows = _db.execute("SELECT * FROM trace WHERE case_id=? AND id>? ORDER BY id LIMIT ?", (case_id, after_id, limit)).fetchall()
    else:
        rows = _db.execute("SELECT * FROM trace WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit)).fetchall()
    return [dict(r) for r in rows]


def start_run(kind: str) -> int:
    with _lock:
        cur = _db.execute("INSERT INTO scenario_runs (kind, status, started) VALUES (?,?,?)", (kind, "running", _now()))
        _db.commit()
        return cur.lastrowid


def finish_run(run_id: int, case_id: str | None, status: str, outcome: str, detail: Any) -> None:
    with _lock:
        _db.execute("UPDATE scenario_runs SET case_id=?, status=?, finished=?, outcome=?, detail=? WHERE id=?",
                    (case_id, status, _now(), outcome, json.dumps(detail, ensure_ascii=False, default=str), run_id))
        _db.commit()


def set_run_case(run_id: int, case_id: str) -> None:
    with _lock:
        _db.execute("UPDATE scenario_runs SET case_id=? WHERE id=?", (case_id, run_id))
        _db.commit()


def list_runs(limit: int = 50) -> list[dict]:
    rows = _db.execute("SELECT * FROM scenario_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def reset_all() -> None:
    with _lock:
        _db.executescript("DELETE FROM cases; DELETE FROM messages; DELETE FROM trace; DELETE FROM scenario_runs;")
        _db.commit()
