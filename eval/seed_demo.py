"""Seeds DEMO cases for the dashboard (synthetic farmers, patterns based on real incidents).

Patterns: Nashik Kharif crops (onion, soybean, grapes, maize, tomato) hit by unseasonal rain and
hail, as in the late-season Nashik losses; Jodhpur Kharif crops (bajra, moong, guar) with
post-harvest rain. Every seeded case has "demo": true and is never acted on by the agent.

Usage:  .venv\\Scripts\\python -m eval.seed_demo [count]      (clears earlier demo cases first)
"""

import json
import random
import sys
from datetime import datetime, timedelta

from app import clock, store

random.seed(72)

PLACES = {
    "Nashik": {"talukas": {"Niphad": "Lasalgaon", "Dindori": "Mohadi", "Satana": "Satana", "Malegaon": "Malegaon", "Chandwad": "Chandwad", "Sinnar": "Sinnar", "Yeola": "Yeola"},
               "crops": [("onion", 34), ("soybean", 24), ("grapes", 14), ("maize", 16), ("tomato", 12)], "lang": [("mr", 80), ("hi", 20)]},
    "Jodhpur": {"talukas": {"Osian": "Tinwari", "Bilara": "Bilara", "Phalodi": "Phalodi", "Bhopalgarh": "Bhopalgarh"},
                "crops": [("bajra", 50), ("moong", 30), ("guar", 20)], "lang": [("mwr", 70), ("hi", 30)]},
}
PERILS = [("unseasonal_rain", 45), ("hailstorm", 30), ("inundation", 12), ("cloudburst", 5), ("natural_fire", 8)]
CHANNELS = [("whatsapp", 68), ("voice", 24), ("sms", 8)]
FIRST = ["Sunita", "Ramesh", "Savita", "Mahesh", "Kamla", "Suresh", "Anita", "Vijay", "Lakshmi", "Bhagirath", "Geeta", "Dnyaneshwar", "Manisha", "Prakash", "Ratan", "Shobha"]
LAST = ["Pawar", "Shinde", "Jadhav", "Patil", "Gaikwad", "More", "Choudhary", "Bishnoi", "Jat", "Mali", "Kale", "Deshmukh"]


def pick(options):
    return random.choices([o for o, _ in options], [w for _, w in options])[0]


def make_case(i: int, now: datetime) -> tuple[dict, str, str]:
    district = random.choices(["Nashik", "Jodhpur"], [72, 28])[0]
    p = PLACES[district]
    taluka = random.choice(list(p["talukas"]))
    crop, peril, channel, lang = pick(p["crops"]), pick(PERILS), pick(CHANNELS), pick(p["lang"])
    days_ago = random.choices(range(18), [1 + d for d in reversed(range(18))])[0]
    created = now - timedelta(days=days_ago, hours=random.uniform(0, 20))
    event = created - timedelta(hours=random.uniform(2, 60))
    dl = event + timedelta(hours=72)
    hours_left = (dl - now).total_seconds() / 3600

    portal_failed = random.random() < 0.18
    roll = random.random()
    if hours_left > 0 and days_ago <= 2 and roll < 0.45:
        stage, status = random.choice([("intake", "not_started"), ("confirm", "not_started"), ("filing", "pending")])
    elif roll < 0.04:
        stage, status = "out_of_scope", "not_started"
    elif roll < 0.07:
        stage, status = "escalated", "pending"
    else:
        stage, status = random.choices([("tracking", "verified"), ("closed", "verified")], [55, 45])[0]

    case_id = f"CG-2026-{i:04d}"
    attempts, log = [], []
    verified_at = None
    if status == "verified":
        minutes = random.uniform(4, 40) + (random.uniform(20, 240) if portal_failed else 0)
        verified_at = created + timedelta(minutes=minutes)
        if portal_failed:
            attempts.append({"route": "portal_api", "result": "failed", "error": random.choice(["timeout", "http"]), "at": (created + timedelta(minutes=minutes / 2)).isoformat(timespec="seconds")})
            log.append({"ts": (created + timedelta(minutes=minutes / 2)).isoformat(timespec="seconds"), "kind": "pending", "text": "Portal submission failed. Status: PENDING, NOT DONE. Background retries continue."})
        attempts.append({"route": "portal_api", "result": "accepted", "at": verified_at.isoformat(timespec="seconds")})
        log.append({"ts": verified_at.isoformat(timespec="seconds"), "kind": "done", "text": "VERIFIED: loss intimation confirmed by status lookup"})
    elif status == "pending" and portal_failed:
        attempts.append({"route": "portal_api", "result": "failed", "error": "timeout", "at": created.isoformat(timespec="seconds")})

    # What went wrong along the way (shown in the dashboard's failure-reasons chart).
    reason = None
    if portal_failed:
        reason = attempts[0]["error"] if attempts else "timeout"
        reason = {"timeout": "API timeout", "http": "Server error"}.get(reason, reason)
    elif random.random() < 0.16:
        reason = random.choices(["Invalid document", "Incomplete info", "Duplicate request", "Other"], [34, 30, 18, 18])[0]

    code = "RJ" if district == "Jodhpur" else "MH"
    state = {
        "id": case_id, "demo": True, "failure_reason": reason, "avatar": i % 5, "phone": f"+9190{random.randint(10000000, 99999999)}", "channel": channel, "language": lang,
        "farmer_name": f"{random.choice(FIRST)} {random.choice(LAST)}", "stage": stage,
        "fields": {"peril": peril, "crop": crop, "district": district, "taluka": taluka, "village": p["talukas"][taluka],
                   "survey_no": f"{random.randint(10, 400)}/{random.randint(1, 4)}", "area_acres": round(random.uniform(1, 5), 1),
                   "application_no": f"{'0402' if code == 'MH' else '0815'}-2026-K-{random.randint(100000, 199999)}"},
        "sources": {}, "conflicts": [], "peril_class": "widespread" if stage == "out_of_scope" else "localised",
        "event_time": event.isoformat(timespec="seconds"), "deadline": dl.isoformat(timespec="seconds"),
        "evidence": [{"type": "damage_photo", "accepted": True}] * random.choice([0, 1, 2, 2]),
        "route_plan": [], "attempts": attempts,
        "intimation": {"status": status, "reference": f"PMFBY-{code}-2610-{random.randint(100000, 999999)}" if status == "verified" else None,
                       "route": "portal_api" if status == "verified" else None, "verified_at": verified_at.isoformat(timespec="seconds") if verified_at else None},
        "handoff": None, "surveyor": {"visit_date": (verified_at + timedelta(days=2)).strftime("%Y-%m-%d")} if stage == "closed" else None,
        "escalations": [{"reason": "Officer did not forward report within hand-off window"}] if stage == "escalated" else [],
        "next_action": None, "action_log": log, "history": [], "created": created.isoformat(timespec="seconds"),
        "awaiting_consent": stage == "confirm", "consent_at": None, "pending_question": None,
    }
    updated = (verified_at or created).isoformat(timespec="seconds")
    return state, created.isoformat(timespec="seconds"), updated


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 842
    store._db.execute("DELETE FROM cases WHERE id LIKE 'CG-2026-%'")
    store._db.commit()
    now = clock.now()
    for i in range(1, count + 1):
        store.insert_case(*make_case(i, now))
    from app import demo_roll
    demo_roll.ANCHOR_FILE.write_text(json.dumps({"anchor": now.isoformat(timespec="seconds")}), encoding="utf-8")  # dates roll forward from here
    print(f"Seeded {count} demo cases")


if __name__ == "__main__":
    main()
