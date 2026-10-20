"""Stage script mode for the live demo phone: fixed farmer steps and fixed agent replies, no AI calls.

Backup for the finale when the AI is slow. It is switched on/off from the dashboard and the phone shows a
'Stage script' badge while it is on, so it is never presented as the live AI.
"""

import asyncio
import json
import random
from datetime import timedelta

from . import agent, clock, store
from .config import DATA_DIR, ROOT

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


# SMS conversation (plain text both ways, no photo, no emoji)
SMS_STEPS = [
    ("नमस्ते, कल रात ओले गिरे, मेरी प्याज की पूरी फसल खराब हो गई।",
     "72 घंटे: नमस्ते सुनीता जी। प्याज की फसल के ओलों से नुकसान की सूचना मिली। 72 घंटे की घड़ी शुरू, आपके पास 2 दिन 19 घंटे हैं। पॉलिसी MH-26-0457812 मिल गई। क्या खेत की फोटो है?"),
    ("खेत की फोटो नहीं है, आगे बढ़िए",
     "72 घंटे: ठीक है। सूचना: प्याज, ओलावृष्टि, 2 एकड़, लासलगाँव, निफाड, नासिक। क्या मैं यह रिपोर्ट दर्ज कर दूँ? उत्तर दें: हाँ / ना"),
    ("हाँ जी, रिपोर्ट दर्ज कर दीजिए",
     "72 घंटे: आपकी रिपोर्ट दर्ज हो गई और बीमा कंपनी ने पुष्टि की। डॉकेट {docket}। सर्वेयर की तारीख SMS से भेजेंगे।"),
]


def is_on() -> bool:
    """The demo phone always uses the fixed flow (Yuvraj's choice for the finale)."""
    return True


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


REAL_TIMEOUT_S = 15  # real agent gets this long per step; slower -> fixed reply (logged as a fallback)


def _media_bytes(url: str | None) -> list[tuple[bytes, str]]:
    if not url:
        return []
    path = ROOT / "dashboard" / url[len("/static/"):]
    mime = "audio/ogg" if path.suffix == ".ogg" else "image/jpeg"
    return [(path.read_bytes(), mime)]


def _last_id() -> int:
    return store._db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]


async def step(phone: str, text: str | None = None, as_text: bool = False, reset=None) -> dict:
    """Plays the next demo step. The REAL agent (Groq/Gemini) answers first; only if it is slower than
    REAL_TIMEOUT_S (or down) the fixed reply is shown, and that fallback is recorded in Agent Logs."""
    ch = "sms" if as_text else "sim"  # SMS tab = its own text conversation; WhatsApp tab = the WhatsApp chat
    case = store.open_case_for(phone)
    done = sum(1 for m in store.messages_for_case(case["id"]) if m["direction"] == "in" and m["channel"] == ch) if case and case.get("stage_script") else 0
    if not case or not case.get("stage_script") or done == 0 or done >= len(STEPS):  # first message of a tab = new case
        if reset:
            await reset(phone)  # closes the old chat and clears the mock portal's earlier filing for this farmer
        case, done = None, 0
    if as_text:
        farmer_text, fixed = SMS_STEPS[done]
        kind, media, farmer_text = "text", None, text or farmer_text
    else:
        kind, farmer_text, media, fixed = STEPS[done]
        if text:
            kind, media, farmer_text = "text", None, text

    # 1) the real agent
    mark, why = _last_id(), None
    if not (case and case.get("fallback")):
        try:
            await asyncio.wait_for(agent.handle_inbound(phone, "sim", farmer_text if kind == "text" else None, _media_bytes(media)), REAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            why = f"AI slower than {REAL_TIMEOUT_S} s"
        except Exception as err:  # noqa: BLE001 - the farmer still gets an answer
            why = f"{type(err).__name__}: {str(err)[:120]}"
    else:
        why = "earlier step used the fallback"
    case = store.open_case_for(phone)
    new = [m for m in (store.messages_for_case(case["id"]) if case else []) if m["id"] > mark]
    replied = any(m["direction"] == "out" for m in new)
    if done == 2 and replied and case and case["intimation"]["status"] != "verified":
        why, replied = "agent did not finish the filing in time", False

    # 2) safety net: fixed reply, honestly logged
    if not replied:
        if case is None:
            case = _fresh_case(phone)
        if not any(m["direction"] == "in" for m in new):
            store.add_message(case["id"], phone, "sim", "in", kind, farmer_text, media)
        now = clock.now().isoformat()
        if done == 2:
            docket = case["intimation"].get("reference") or DOCKET_PREFIX + str(random.randint(100000, 999999))
            fixed = fixed.format(docket=docket)
            case["consent_at"] = case.get("consent_at") or now
            case["attempts"].append({"at": now, "route": "portal_api", "result": "accepted", "error": None})
            case["intimation"].update({"status": "verified", "reference": docket, "route": "portal_api", "verified_at": now})
            case["stage"] = "tracking"
            if ch == "sim":
                store.add_message(case["id"], phone, "sms", "out", "text", f"72 घंटे: आपकी फसल नुकसान की सूचना दर्ज हुई। डॉकेट {docket}। सर्वेयर की तारीख जल्द भेजेंगे।")
        store.add_message(case["id"], phone, "sim", "out", "text", fixed)
        case["fallback"] = True
        store.add_trace(case["id"], "decision", "fallback_reply", "warn", None, {"step": done + 1}, {"reason": why or "no reply", "reply": "fixed demo reply"})
        case["action_log"].append({"ts": now, "kind": "known", "text": f"Fallback reply used ({why or 'no reply'})"})

    case = store.open_case_for(phone) if not replied else case
    if case is None:
        return {"case_id": None, "step": done + 1}
    case["stage_script"] = True
    store.save_case(case)
    if ch == "sms":  # the SMS tab is its own conversation
        store._db.execute("DELETE FROM messages WHERE id>? AND case_id=? AND channel='sms'", (mark, case["id"]))  # SMS copies would duplicate here
        store._db.execute("UPDATE messages SET channel='sms' WHERE id>? AND case_id=? AND channel='sim'", (mark, case["id"]))
        store._db.commit()
    return {"case_id": case["id"], "step": done + 1, "real_ai": replied}
