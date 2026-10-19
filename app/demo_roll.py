"""Keeps the demo cases (CG-2026-*) current.

The 842 demo cases were generated once with fixed dates, so their 72-hour deadlines would all run out a
couple of days later and the dashboard would show no open cases. Every 6 hours this moves every date
inside every demo case forward by the time that has passed, in whole 6-hour steps. The cases, numbers
and stories stay exactly the same; only the dates follow today. Live and test cases are never touched.
"""

import json
import re
from datetime import timedelta

from . import clock, store
from .config import DATA_DIR

STEP = timedelta(hours=6)
ANCHOR_FILE = DATA_DIR / "demo_anchor.json"
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d")


def _shift(value, delta):
    if isinstance(value, str) and _TS.match(value):
        t = clock.parse(value)
        return (t + delta).isoformat(timespec="seconds") if t else value
    if isinstance(value, dict):
        return {k: _shift(v, delta) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift(v, delta) for v in value]
    return value


def roll() -> int:
    """Shift demo cases forward if at least one 6-hour step has passed. Returns the number of steps applied."""
    now = clock.now()
    if not ANCHOR_FILE.exists():
        ANCHOR_FILE.write_text(json.dumps({"anchor": now.isoformat(timespec="seconds")}), encoding="utf-8")
        return 0
    anchor = clock.parse(json.loads(ANCHOR_FILE.read_text(encoding="utf-8"))["anchor"])
    steps = int((now - anchor) / STEP)
    if steps <= 0:
        return 0
    delta = STEP * steps
    rows = store._db.execute("SELECT id, state FROM cases WHERE id LIKE 'CG-2026-%'").fetchall()
    with store._lock:
        for row in rows:
            state = _shift(json.loads(row["state"]), delta)
            store._db.execute("UPDATE cases SET state=?, created=? WHERE id=?",
                              (json.dumps(state, ensure_ascii=False), state.get("created") or "", row["id"]))
        store._db.commit()
    ANCHOR_FILE.write_text(json.dumps({"anchor": (anchor + delta).isoformat(timespec="seconds")}), encoding="utf-8")
    return steps
