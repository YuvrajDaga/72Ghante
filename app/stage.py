"""Stage script mode for the live demo phone: fixed farmer steps and fixed agent replies, no AI calls.

Backup for the finale when the AI is slow. It is switched on/off from the dashboard and the phone shows a
'Stage script' badge while it is on, so it is never presented as the live AI.
"""

import asyncio
import json
import random
from datetime import timedelta

from . import clock, store
from .config import DATA_DIR

MODE_FILE = DATA_DIR / "stage_mode.json"
DEMO = "/static/demo/"
DOCKET_PREFIX = "PMFBY-MH-2610-"

# farmer step -> (kind, farmer text, media) ; agent reply
STEPS = [
    ("voice", "नमस्ते, कल रात ओले गिरे, मेरी प्याज की पूरी फसल खराब हो गई। मदद कीजिए।", DEMO + "farmer_voice1.ogg",
     "नमस्ते सुनीता जी 🙏 आपकी प्याज की फसल के ओलों से नुकसान की सूचना मिली।\n⏱ 72 घंटे की घड़ी शुरू — आपके पास 2 दिन 19 घंटे हैं।\n📄 आपकी पॉलिसी मिल गई (MH-26-0457812)।\nकृपया खेत की 1 फोटो भेजें।"),
    ("image", "", DEMO + "field_photo.jpg",
     "फोटो मिल गई ✅ नुकसान साफ दिख रहा है।\nआपकी सूचना: प्याज · ओलावृष्टि · 2 एकड़ · लासलगाँव, निफाड, नासिक।\nक्या मैं यह रिपोर्ट दर्ज कर दूँ? हाँ / ना"),
    ("voice", "हाँ जी, हाँ… रिपोर्ट दर्ज कर दीजिए।", DEMO + "farmer_yes.ogg",
     "✅ आपकी रिपोर्ट दर्ज हो गई और बीमा कंपनी ने पुष्टि की।\nडॉकेट नंबर: {docket}\nसर्वेयर की तारीख SMS से बताएंगे।"),
]


def is_on() -> bool:
    try:
        return bool(json.loads(MODE_FILE.read_text()).get("script"))
    except (OSError, ValueError):
        return False


def set_on(on: bool) -> None:
    MODE_FILE.write_text(json.dumps({"script": bool(on)}))


def clear(phone: str) -> None:
    """Empty chat on the demo phone: close its open case (history stays in Cases / Agent Logs)."""
    old = store.open_case_for(phone)
    if old:
        old["stage"] = "closed"
        store.save_case(old)


def _fresh_case(phone: str) -> dict:
    old = store.open_case_for(phone)
    if old:
        old["stage"] = "closed"
        store.save_case(old)
    case = store.new_case(phone, "sim")
    ev = clock.now() - timedelta(hours=5)
    case.update({"farmer_name": "Sunita Pawar", "language": "hi", "stage": "collecting", "stage_script": True,
                 "event_time": ev.isoformat(), "deadline": (ev + timedelta(hours=72)).isoformat(), "peril_class": "localised"})
    case["fields"].update({"crop": "onion", "peril": "hailstorm", "district": "Nashik", "taluka": "Niphad", "village": "Lasalgaon",
                           "area_acres": 2, "survey_no": "112/2", "application_no": "MH-26-0457812", "event_time": ev.isoformat()})
    case["action_log"].append({"ts": clock.now().isoformat(), "kind": "known", "text": "Stage script demo: case opened from a voice note"})
    store.save_case(case)
    return case


async def step(phone: str, text: str | None = None, as_text: bool = False) -> dict:
    """Plays the next scripted step. Typed text (or SMS mode) replaces the farmer's media with that text."""
    case = store.open_case_for(phone)
    done = sum(1 for m in store.messages_for_case(case["id"]) if m["direction"] == "in") if case and case.get("stage_script") else 0
    if not case or not case.get("stage_script") or done >= len(STEPS):
        case, done = _fresh_case(phone), 0
    kind, farmer_text, media, reply = STEPS[done]
    if text or as_text:
        kind, media, farmer_text = "text", None, text or farmer_text or "फोटो नहीं है, आगे बढ़िए"
    store.add_message(case["id"], phone, "sim", "in", kind, farmer_text, media)
    await asyncio.sleep(2.2)  # agent 'typing…'
    now = clock.now().isoformat()
    if done == 2:
        docket = DOCKET_PREFIX + str(random.randint(100000, 999999))
        reply = reply.format(docket=docket)
        case["consent_at"] = now
        case["attempts"].append({"at": now, "route": "portal_api", "result": "accepted", "error": None})
        case["intimation"].update({"status": "verified", "reference": docket, "route": "portal_api", "verified_at": now})
        case["stage"] = "tracking"
        case["action_log"].append({"ts": now, "kind": "done", "text": f"Stage script demo: report filed and verified ({docket})"})
        store.add_message(case["id"], phone, "sms", "out", "text", f"72 घंटे: आपकी फसल नुकसान की सूचना दर्ज हुई। डॉकेट {docket}। सर्वेयर की तारीख जल्द भेजेंगे।")
    store.add_message(case["id"], phone, "sim", "out", "text", reply)
    store.save_case(case)
    return {"case_id": case["id"], "step": done + 1}
