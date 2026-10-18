"""72 Ghante agent: owns one farmer's crop-loss case from first voice note to surveyor visit.

Loop per inbound message:  perceive (speech / photo / text)  ->  understand (Gemini)
-> update case state  ->  plan the next step  ->  act with tools  ->  verify  ->  reply.
A background tick keeps every open case moving even when the farmer is silent: retries,
receipt verification, hand-off clocks, surveyor tracking and escalation.

Hard rules enforced in code (not left to the LLM):
- nothing is filed without explicit consent after a full read-back;
- "done" is only said after the docket is verified with a status lookup;
- the deadline is computed by code; urgency changes the plan.
"""

import asyncio
from collections import defaultdict
from datetime import timedelta

from . import channels, clock, deadline, llm, replies, routes, store, tools
from .config import HANDOFF_WINDOW_HOURS
from .understand import understand

REQUIRED = ["peril", "event_time", "crop", "application_no", "survey_no"]
QUESTIONS = {
    "peril": "what happened to the crop (hail, unseasonal rain, fire, flood...)",
    "event_time": "when exactly did it happen (which day, what time)",
    "crop": "which crop was damaged",
    "crop_harvested": "was the crop already harvested and lying in the field, or still standing",
    "application_no": "the PMFBY application number, or a photo of the insurance receipt or bank passbook",
    "survey_no": "the survey / gat number of the damaged field",
    "plot_choice": "which field was damaged",
}
SUBMIT_RETRIES = 3

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# ------------------------------------------------------------------ helpers

def log(case: dict, kind: str, text: str) -> None:
    """kind: known (farmer said) | retrieved (from a tool) | done (verified action) | pending (attempted, not confirmed)."""
    case["action_log"].append({"ts": clock.now().isoformat(timespec="seconds"), "kind": kind, "text": text})


def set_field(case: dict, name: str, value, source: str) -> None:
    if value in (None, ""):
        return
    old = case["fields"].get(name)
    if old is not None and str(old) != str(value) and case["sources"].get(name) != source:
        case["conflicts"].append({"field": name, "values": {case["sources"].get(name): old, source: value}, "resolved": False})
        store.add_trace(case["id"], "decision", "conflict_detected", "warn", None, {"field": name}, {"existing": old, "new": value, "source": source})
        return
    case["fields"][name] = value
    case["sources"][name] = source


def hours_left(case: dict) -> float | None:
    dl = clock.parse(case.get("deadline"))
    return deadline.hours_left(dl) if dl else None


def tier(case: dict) -> str:
    h = hours_left(case)
    return deadline.urgency(h) if h is not None else "normal"


def numbers_line(case: dict) -> str:
    """Exact numbers written by code, appended to replies (never phrased by the LLM)."""
    parts = []
    h = hours_left(case)
    if h is not None and case["intimation"]["status"] != "verified":
        parts.append(f"⏱ {deadline.describe(h)}")
    if case["intimation"]["reference"] and case["intimation"]["status"] == "verified":
        parts.append(f"📄 Docket: {case['intimation']['reference']}")
    parts.append(f"Case: {case['id']}")
    return " · ".join(parts)


async def say(case: dict, key: str, facts: dict, sms: bool = False) -> None:
    facts = {**facts, "status": case["intimation"]["status"], "first_message": not any(h["role"] == "agent" for h in case["history"])}
    text = await replies.compose(case, key, facts)
    await channels.send(case, f"{text}\n\n{numbers_line(case)}", also_sms=sms)
    case["history"].append({"role": "agent", "text": text})


def next_action(case: dict, what: str, owner: str, due_hours: float | None) -> None:
    due = (clock.now() + timedelta(hours=due_hours)).isoformat(timespec="minutes") if due_hours else None
    case["next_action"] = {"what": what, "owner": owner, "due": due}


# ------------------------------------------------------------------ inbound

async def handle_inbound(phone: str, channel: str, text: str | None, media: list[tuple[bytes, str]]) -> dict:
    async with _locks[phone]:
        case = store.open_case_for(phone) or store.new_case(phone, channel)
        case["channel"] = channel
        store.add_trace(case["id"], "inbound", f"{channel}.message", "ok", None, {"text": text, "media": [m for _, m in media]}, None)
        heard: list[str] = []
        if text:
            heard.append(text)
            store.add_message(case["id"], phone, channel, "in", "text", text)

        for data, mime in media:
            if mime.startswith("audio"):
                path = channels.save_media(data, "ogg" if "ogg" in mime else "bin")
                try:
                    t = await llm.transcribe(data, mime, case["id"])
                except llm.LLMUnavailable:
                    store.add_message(case["id"], phone, channel, "in", "voice", "[could not transcribe]", path)
                    await channels.send(case, "Sorry, I could not hear the voice note. Please send it again or type the message.", voice=False)
                    store.save_case(case)
                    return case
                store.add_message(case["id"], phone, channel, "in", "voice", t.get("transcript"), path)
                case["language"] = case["language"] or t.get("language")
                if t.get("audio_quality") == "poor" or not (t.get("transcript") or "").strip():
                    await say(case, "audio_unclear", {"ask": "please send the voice note again from a quieter place"})
                    store.save_case(case)
                    return case
                heard.append(t["transcript"])
            elif mime.startswith("image"):
                path = channels.save_media(data, "jpg")
                store.add_message(case["id"], phone, channel, "in", "image", None, path)
                await handle_image(case, data, mime, path)

        if heard:
            await handle_text(case, " ".join(heard))
        await advance(case)
        store.save_case(case)
        return case


async def handle_text(case: dict, text: str) -> None:
    case["history"].append({"role": "farmer", "text": text})
    try:
        nlu = await understand(text, {"pending_question": case["pending_question"], "awaiting_consent": case["awaiting_consent"], "fields": case["fields"]}, case["id"])
    except llm.LLMUnavailable:
        log(case, "pending", "Could not understand the message (AI unavailable); asked farmer to repeat")
        case["_reply_override"] = ("ask_repeat", {"reason": "temporary problem understanding"})
        return
    case["language"] = case["language"] or nlu.get("language")
    case["_nlu"] = nlu
    intent = nlu.get("intent")

    if intent == "correction" or (case["awaiting_consent"] and intent not in ("consent_yes", "consent_no")):
        # Any change after the read-back invalidates the earlier read-back.
        case["awaiting_consent"] = False
    for f in ("peril", "crop", "survey_no", "application_no", "area_acres", "village"):
        val = nlu.get(f)
        if val not in (None, ""):
            if intent == "correction":
                case["fields"][f] = val
                case["sources"][f] = "farmer"
                case["conflicts"] = [c for c in case["conflicts"] if c["field"] != f]
            else:
                set_field(case, f, val, "farmer")
            log(case, "known", f"Farmer said {f} = {val}")
    if nlu.get("crop_harvested") is not None:
        case["fields"]["crop_harvested"] = nlu["crop_harvested"]
    ev = clock.parse(nlu.get("event_time")) if nlu.get("event_time") else None
    if ev and not (clock.now() - timedelta(days=30) <= ev <= clock.now() + timedelta(minutes=10)):
        # A resolved time in the future or months ago is a model error, not a fact: never start the clock on it.
        store.add_trace(case["id"], "decision", "reject_event_time", "warn", None, {"expression": nlu.get("event_time_expression")}, {"resolved": nlu.get("event_time")})
        nlu["event_time"] = None
    if nlu.get("event_time") and (nlu.get("event_time_confidence", 0) >= 0.5 or case["pending_question"] == "event_time"):
        case["fields"]["event_time"] = nlu["event_time"]
        case["sources"]["event_time"] = "farmer"
        log(case, "known", f"Event time from '{nlu.get('event_time_expression')}' → {nlu['event_time']} (confidence {nlu.get('event_time_confidence')})")
    if nlu.get("damage_extent"):
        case["fields"]["damage_description"] = nlu["damage_extent"]

    # Resolve a pending plot choice by survey number or crop.
    if case["pending_question"] == "plot_choice" and case.get("plots"):
        for p in case["plots"]:
            if nlu.get("survey_no") == p["survey_no"] or (nlu.get("crop") or "").lower() == p["crop"]:
                case["fields"]["survey_no"], case["fields"]["crop"] = p["survey_no"], p["crop"]
                case["sources"]["survey_no"] = case["sources"]["crop"] = "registry"
                case["fields"]["area_acres"] = p["area_acres"]
    # A conflict answered by the farmer is resolved with the farmer's latest statement.
    if case["pending_question"] and case["pending_question"].startswith("conflict:"):
        field = case["pending_question"].split(":", 1)[1]
        val = nlu.get(field)
        if val:
            case["fields"][field] = val
            case["sources"][field] = "farmer (confirmed)"
            for c in case["conflicts"]:
                if c["field"] == field:
                    c["resolved"] = True
            log(case, "known", f"Conflict on {field} resolved by farmer: {val}")
            plot = next((p for p in case.get("insured_plots", []) if field == "survey_no" and p["survey_no"] == val), None)
            if plot:
                case["fields"]["area_acres"] = plot["area_acres"]
                log(case, "retrieved", f"Confirmed survey {val} is insured: {plot['crop']}, {plot['area_acres']} acre")

    if intent == "consent_yes" and case["awaiting_consent"]:
        case["consent_at"] = clock.now().isoformat(timespec="seconds")
        log(case, "known", "Farmer gave explicit consent to submit after read-back")
        store.add_trace(case["id"], "decision", "consent_received", "ok", None, None, {"at": case["consent_at"]})
    elif intent == "consent_no" and case["awaiting_consent"]:
        case["awaiting_consent"] = False
        case["_reply_override"] = ("consent_declined", {"ask": "what should be corrected"})
    elif intent == "skip" and case["pending_question"] == "photo":
        case["photo_skipped"] = True
    elif intent == "status_query":
        case["_reply_override"] = ("status_update", status_facts(case))
    elif intent == "question" and nlu.get("question_text"):
        case["_question"] = nlu["question_text"]
    if nlu.get("ticket_no") and case.get("handoff") and case["handoff"]["route"] == "helpline_14447":
        case["handoff"]["confirmed"] = True
        case["handoff"]["ticket_no"] = nlu["ticket_no"]
        log(case, "known", f"Farmer reports helpline ticket {nlu['ticket_no']} (farmer-provided, not independently verified)")
        case["_reply_override"] = ("helpline_ticket_noted", {"ticket": nlu["ticket_no"], "note": "saved; I still keep trying to get a portal-verified docket"})
    if nlu.get("surveyor_visited") is True and case["stage"] == "tracking":
        case["stage"] = "closed"
        log(case, "done", "Farmer confirmed surveyor visit. Case closed.")
        next_action(case, "None: case closed after surveyor visit", "-", None)
        case["_reply_override"] = ("case_closed", {"docket": case["intimation"]["reference"], "note": "if there is any problem with the claim later, message me again"})
    elif nlu.get("surveyor_visited") is False and case["stage"] == "tracking":
        case["_escalate"] = "Farmer reports surveyor did not visit"


async def handle_image(case: dict, data: bytes, mime: str, path: str, use_vision: bool = True) -> None:
    quality = await asyncio.to_thread(tools.photo_quality, data)
    store.add_trace(case["id"], "tool", "photo.quality_check", "ok" if not quality["problems"] else "warn", None, None, quality)
    if not use_vision:
        seen = {"kind": "crop_damage_photo", "damage_visible": not quality["problems"], "summary": "vision skipped (deterministic test)"}
    else:
        try:
            seen = await llm.analyse_image(data, mime, case["id"])
        except llm.LLMUnavailable:
            seen = {"kind": "other", "readable": False, "problems": ["could not analyse"]}

    if seen.get("kind") in ("insurance_document", "bank_passbook"):
        doc = seen.get("document_fields") or {}
        found = [k for k, v in doc.items() if v]
        for k in ("application_no", "survey_no", "crop", "area_acres"):
            if doc.get(k):
                set_field(case, k, str(doc[k]).lower() if k == "crop" else doc[k], "document")
        if doc.get("insurer"):
            case["fields"]["insurer"] = doc["insurer"]
        case["evidence"].append({"type": "document", "path": path, "accepted": bool(found), "reason": f"read: {', '.join(found)}" if found else "nothing readable"})
        log(case, "retrieved", f"Read document photo: {', '.join(found) or 'nothing readable'}")
        if not found:
            case["_reply_override"] = ("document_unreadable", {"problems": seen.get("problems") or quality["problems"]})
        return

    problems = list(quality["problems"])
    if seen.get("kind") == "crop_damage_photo" and not seen.get("damage_visible"):
        problems.append("no visible damage")
    if seen.get("kind") == "other":
        problems.append("not a photo of the field")
    accepted = not problems
    case["evidence"].append({
        "type": "damage_photo", "path": path, "accepted": accepted,
        "reason": "usable: " + (seen.get("damage_description") or "damage visible") if accepted else "rejected: " + ", ".join(problems),
        "received_at": clock.now().isoformat(timespec="seconds"),
        "note": "WhatsApp removes photo time/GPS; receipt time recorded instead",
    })
    log(case, "retrieved" if accepted else "pending", f"Damage photo {'accepted' if accepted else 'rejected (' + ', '.join(problems) + ')'}")
    store.add_trace(case["id"], "decision", "photo_admissibility", "ok" if accepted else "rejected", None, {"quality": quality["problems"], "vision": seen.get("summary")}, {"accepted": accepted})
    if not accepted:
        case["_reply_override"] = ("photo_rejected", {"problems": problems, "how_to_fix": "take the photo in daylight, close to the damaged crop, holding the phone steady"})
    elif case["intimation"]["status"] == "verified":
        case["_reply_override"] = ("photo_added_after_filing", {"note": "photo saved with your case for the surveyor"})


# ------------------------------------------------------------------ planning

async def advance(case: dict) -> None:
    """Decide and take the next step for this case."""
    override = case.pop("_reply_override", None)
    question = case.pop("_question", None)
    case.pop("_nlu", None)
    f = case["fields"]

    escalation = case.pop("_escalate", None)
    if escalation:
        return await escalate(case, escalation)
    if override:
        return await say(case, override[0], override[1])

    if question:
        await say(case, "answer_question", {"question": question, "case_status": status_facts(case),
                                            "rule": "answer briefly from general PMFBY process knowledge; if unsure say an officer will confirm"})
        if case["stage"] not in ("intake", "confirm"):
            return

    if case["stage"] in ("filing", "tracking", "closed"):
        if not question:
            await say(case, "status_update", status_facts(case))
        return  # the background tick keeps these cases moving

    # 1. Peril regime
    if f.get("peril"):
        regime = deadline.classify_peril(f["peril"], f.get("crop_harvested"))
        if regime != case["peril_class"]:
            case["peril_class"] = regime
            store.add_trace(case["id"], "decision", "classify_peril", "ok", None, {"peril": f["peril"], "harvested": f.get("crop_harvested")}, {"regime": regime})
        if regime == "widespread":
            case["stage"] = "out_of_scope"
            log(case, "done", "Loss is widespread: assessed on area basis, no individual 72-hour intimation needed")
            await say(case, "widespread_loss", {"peril": f["peril"], "explain": "this kind of loss is assessed for the whole area by the government (yield survey), so no individual 72-hour report is needed",
                                                "next": "contact the agriculture assistant to confirm the area notification"})
            return
        if regime == "needs_info":
            return await ask(case, "crop_harvested")

    # 2. Clock
    if f.get("event_time") and not case.get("event_time"):
        ev = clock.parse(f["event_time"])
        case["event_time"] = ev.isoformat()
        case["deadline"] = deadline.deadline_for(ev).isoformat()
        h = hours_left(case)
        log(case, "known", f"72-hour clock started: deadline {case['deadline'][:16]} ({deadline.describe(h)})")
        store.add_trace(case["id"], "decision", "start_clock", "ok", None, {"event_time": case["event_time"]}, {"deadline": case["deadline"], "hours_left": round(h, 2), "tier": deadline.urgency(h)})
        if h <= 0:
            return await escalate(case, "72-hour window already closed when farmer reached us")

    # 3. Policy reconstruction from the farmer's mobile number
    if not case.get("policy_checked"):
        case["policy_checked"] = True
        try:
            res = await tools.lookup_policy(case["id"], case["phone"])
        except tools.ToolError:
            res = {"found": False, "error": True}
            log(case, "pending", "Policy lookup failed (service error); will ask farmer for receipt photo")
        if res.get("found"):
            apply_registry(case, res["farmer"])

    # 4. Weather corroboration (signal only)
    if case.get("event_time") and "weather" not in case:
        case["weather"] = await tools.weather_check(case["id"], f.get("village") or case.get("village") or "nashik", case["event_time"])
        if case["weather"].get("available"):
            log(case, "retrieved", f"Live weather near {case['weather']['place']}: {case['weather']['rain_mm_24h']} mm rain around event time"
                + (", hail signal" if case["weather"]["hail_signal"] else "") + " (signal only, never blocks the claim)")

    # 5. Missing information: ask only for the gaps, one at a time
    unresolved = [c for c in case["conflicts"] if not c["resolved"]]
    if unresolved:
        c = unresolved[0]
        case["pending_question"] = f"conflict:{c['field']}"
        await say(case, "ask_conflict", {"field": c["field"], "values_seen": c["values"], "ask": "which is correct"})
        return
    if case.get("plots") and not f.get("survey_no"):
        case["pending_question"] = "plot_choice"
        await say(case, "ask_field", {"question": QUESTIONS["plot_choice"], "options": [f"{p['crop']} (gat {p['survey_no']}, {p['area_acres']} acre)" for p in case["plots"]]})
        return
    for name in REQUIRED:
        if not f.get(name):
            return await ask(case, name)

    # 6. Evidence: in normal time ask for a photo; when time is short, file first and collect photos after.
    t = tier(case)
    has_photo = any(e["type"] == "damage_photo" and e["accepted"] for e in case["evidence"])
    if not has_photo and not case.get("photo_skipped") and t == "normal" and case["channel"] != "voice":
        if case["pending_question"] != "photo":
            case["pending_question"] = "photo"
            await say(case, "ask_photo", {"ask": "1 or 2 clear photos of the damaged crop in daylight", "optional": "photos help the surveyor; you can also say skip"})
            return
        return  # waiting for photo

    # 7. Read-back and explicit consent
    case.setdefault("info_complete_at", clock.now().isoformat(timespec="seconds"))
    if not case["consent_at"]:
        case["pending_question"] = None
        case["awaiting_consent"] = True
        case["stage"] = "confirm"
        summary = readback(case)
        if t != "normal":
            log(case, "known", f"Urgency {t}: photos will be collected after filing to save time")
        await say(case, "confirm_readback", {"summary": summary, "ask": "Shall I submit this loss report? Say YES or NO", "urgency": t})
        return

    # 8. Act
    case["awaiting_consent"] = False
    case["stage"] = "filing"
    await say(case, "filing_started", {"route": "official PMFBY portal", "note": "I will tell you only after I confirm it was received"})
    await file_case(case)


async def ask(case: dict, name: str) -> None:
    case["pending_question"] = name
    store.add_trace(case["id"], "decision", "ask_missing_field", "ok", None, {"have": list(case["fields"])}, {"asking": name})
    facts = {"question": QUESTIONS[name]}
    if tier(case) in ("hurry", "critical"):
        facts["urgency"] = f"only {deadline.describe(hours_left(case))}, keep it very short"
    await say(case, "ask_field", facts)


def apply_registry(case: dict, farmer: dict) -> None:
    policy = farmer["policies"][0]
    case["village"] = farmer.get("village")
    case["farmer_name"] = farmer.get("name")
    set_field(case, "application_no", policy["application_no"], "registry")
    case["fields"]["insurer"] = policy["insurer"]
    case["fields"]["bank"] = policy["bank"]
    case["fields"]["district"] = farmer["district"]
    case["fields"].setdefault("village", farmer["village"])
    plots = policy["plots"]
    case["insured_plots"] = plots
    log(case, "retrieved", f"Policy found by mobile number: {policy['application_no']} ({policy['insurer']}), {len(plots)} insured plot(s)")
    said_crop = (case["fields"].get("crop") or "").lower()
    said_survey = case["fields"].get("survey_no")
    match = [p for p in plots if p["survey_no"] == said_survey] or [p for p in plots if p["crop"] == said_crop]
    if said_survey and not [p for p in plots if p["survey_no"] == said_survey]:
        case["conflicts"].append({"field": "survey_no", "values": {"farmer": said_survey, "registry": [p["survey_no"] for p in plots]}, "resolved": False})
        store.add_trace(case["id"], "decision", "conflict_detected", "warn", None, {"field": "survey_no"}, {"farmer": said_survey, "insured": [p["survey_no"] for p in plots]})
        case["fields"].pop("survey_no", None)
        return
    if len(match) == 1 or len(plots) == 1:
        p = match[0] if match else plots[0]
        if said_crop and p["crop"] != said_crop:
            case["conflicts"].append({"field": "crop", "values": {"farmer": said_crop, "registry": p["crop"]}, "resolved": False})
        case["fields"]["survey_no"] = p["survey_no"]
        case["sources"]["survey_no"] = "registry"
        case["fields"].setdefault("crop", p["crop"])
        case["fields"]["area_acres"] = p["area_acres"]
    else:
        case["plots"] = plots


def readback(case: dict) -> str:
    f = case["fields"]
    ev = clock.parse(case.get("event_time"))
    return (f"Loss: {f.get('peril')} · Crop: {f.get('crop')} · Field gat no. {f.get('survey_no')} ({f.get('area_acres', '?')} acre) · "
            f"When: {ev:%d %b, %I %p} · Application: {f.get('application_no')} · Insurer: {f.get('insurer', 'from receipt')} · "
            f"Photos: {sum(1 for e in case['evidence'] if e['type'] == 'damage_photo' and e['accepted'])}")


def status_facts(case: dict) -> dict:
    return {"stage": case["stage"], "report_status": case["intimation"]["status"], "docket": case["intimation"]["reference"],
            "next_action": case["next_action"], "surveyor": case["surveyor"]}


# ------------------------------------------------------------------ acting

def intimation_payload(case: dict) -> dict:
    f = case["fields"]
    return {
        "application_no": f["application_no"], "survey_no": f["survey_no"], "crop": f["crop"], "peril": f["peril"],
        "event_time": case["event_time"], "reported_via": "72ghante_agent",
        "photos": sum(1 for e in case["evidence"] if e["type"] == "damage_photo" and e["accepted"]),
        "idempotency_key": case["id"],
    }


async def file_case(case: dict) -> None:
    """Plan the route, try it, verify. On failure: say 'pending, not done', open a fallback route,
    and leave the portal retry to the background tick."""
    healthy = await tools.portal_health(case["id"])
    failed = {a["route"] for a in case["attempts"] if a["result"] == "failed"}
    plan = routes.plan(healthy, hours_left(case) or 72, failed)
    case["route_plan"] = plan
    store.add_trace(case["id"], "decision", "plan_route", "ok", None, {"portal_healthy": healthy, "hours_left": round(hours_left(case) or 0, 1)}, plan)

    if plan[0]["route"] == "portal_api" or healthy:
        if await try_portal(case):
            return
    # Portal failed or unhealthy: the farmer must hear the truth, and a human route opens in parallel.
    case["intimation"]["status"] = "pending"
    log(case, "pending", "Portal submission failed. Status: PENDING, NOT DONE. Background retries continue.")
    fallback = next((r for r in case["route_plan"] if r["route"] != "portal_api"), None)
    await say(case, "pending_not_done", {"what_failed": "the PMFBY portal is not responding", "retrying": True,
                                          "fallback": fallback["label"] if fallback else None})
    if fallback:
        await open_fallback(case, fallback["route"])
    case["next_retry"] = (clock.now() + timedelta(seconds=20)).isoformat()


async def try_portal(case: dict) -> bool:
    payload = intimation_payload(case)
    for attempt in range(1, SUBMIT_RETRIES + 1):
        try:
            res = await tools.submit_intimation(case["id"], payload)
            docket = res["docket_no"]
            case["attempts"].append({"route": "portal_api", "at": clock.now().isoformat(timespec="seconds"), "result": "accepted", "docket": docket})
            log(case, "pending", f"Portal accepted submission, docket {docket}. Verifying before telling the farmer...")
            return await verify(case, docket, "portal_api")
        except tools.ToolError as err:
            if err.status == 409 and err.body and err.body.get("docket_no"):
                # Duplicate: the report already exists. Do not file twice; verify the existing one.
                log(case, "retrieved", f"Duplicate detected: report already exists as {err.body['docket_no']}. Not filing again.")
                case["attempts"].append({"route": "portal_api", "at": clock.now().isoformat(timespec="seconds"), "result": "duplicate", "docket": err.body["docket_no"]})
                return await verify(case, err.body["docket_no"], "portal_api")
            if err.status == 422:
                # Data problem: retrying will not help. Ask the farmer instead of guessing.
                case["attempts"].append({"route": "portal_api", "at": clock.now().isoformat(timespec="seconds"), "result": "rejected", "error": err.body})
                code = (err.body or {}).get("error")
                field = "survey_no" if code == "SURVEY_MISMATCH" else "application_no"
                case["fields"].pop(field, None)
                case["consent_at"] = None
                case["stage"] = "intake"
                log(case, "pending", f"Portal rejected the data ({code}). Asking farmer to check {field}.")
                await say(case, "data_rejected", {"problem": code, "ask": QUESTIONS[field]})
                case["pending_question"] = field
                return True
            case["attempts"].append({"route": "portal_api", "at": clock.now().isoformat(timespec="seconds"), "result": "failed", "error": err.kind, "attempt": attempt})
            store.add_trace(case["id"], "decision", "retry_backoff", "warn", None, {"attempt": attempt}, {"wait_s": 2 ** (attempt - 1)})
            if attempt < SUBMIT_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))
    return False


async def verify(case: dict, docket: str, route: str) -> bool:
    """A docket is only 'done' after an independent status lookup confirms it."""
    try:
        st = await tools.intimation_status(case["id"], docket)
    except tools.ToolError:
        case["intimation"].update({"status": "submitted_unverified", "reference": docket, "route": route})
        log(case, "pending", f"Docket {docket} received but status lookup failed. NOT reporting done yet.")
        case["next_verify"] = (clock.now() + timedelta(seconds=20)).isoformat()
        return True
    case["intimation"].update({"status": "verified", "reference": docket, "route": route, "verified_at": clock.now().isoformat(timespec="seconds"),
                               "late": st.get("late")})
    case["stage"] = "tracking"
    log(case, "done", f"VERIFIED: loss intimation {docket} confirmed by status lookup ({st['status']})")
    store.add_trace(case["id"], "decision", "verified_done", "ok", None, {"docket": docket}, st)
    next_action(case, "Surveyor to be assigned by insurer; agent checks status", "Insurer (agent tracking)", 48)
    if case.get("handoff") and not case["handoff"].get("closed"):
        case["handoff"]["closed"] = "superseded by verified portal docket"
    await say(case, "verified_done", {"docket": docket, "read_slowly": True, "next": "the insurer will assign a surveyor; I will keep checking and tell you",
                                      "photos_later": not any(e["type"] == "damage_photo" and e["accepted"] for e in case["evidence"])}, sms=True)
    return True


async def open_fallback(case: dict, route: str) -> None:
    f = case["fields"]
    if route == "helpline_14447":
        script = (f"Call 14447 (free). Say: 'I want to report crop loss under PMFBY. Application number {f['application_no']}, "
                  f"survey number {f['survey_no']}, crop {f['crop']}, loss due to {f['peril']} on {case['event_time'][:10]}.' "
                  f"Write down the ticket number they give and send it to me.")
        case["handoff"] = {"route": route, "opened": clock.now().isoformat(timespec="seconds"), "script": script, "confirmed": False}
        log(case, "pending", "Fallback opened: helpline call script prepared for farmer (human hand-off, not yet confirmed)")
        next_action(case, "Farmer calls 14447 and shares ticket number (portal retries continue)", "Farmer", 2)
        await say(case, "fallback_helpline", {"script": script, "note": "I keep trying the portal at the same time; whichever confirms first counts"})
    else:
        try:
            res = await tools.officer_report(case["id"], intimation_payload(case))
        except tools.ToolError:
            return
        due = clock.now() + timedelta(hours=min(HANDOFF_WINDOW_HOURS, max((hours_left(case) or 0) - 2, 1)))
        case["handoff"] = {"route": route, "report_id": res["report_id"], "opened": clock.now().isoformat(timespec="seconds"), "due": due.isoformat(), "confirmed": False}
        log(case, "pending", f"Report {res['report_id']} handed to {routes.ROUTES[route]['label']}; tracking onward forwarding until {due:%d %b %H:%M}")
        next_action(case, "Officer forwards report to insurer", routes.ROUTES[route]["label"], (due - clock.now()).total_seconds() / 3600)
        await say(case, "fallback_officer", {"office": routes.ROUTES[route]["label"], "report_id": res["report_id"], "tracking_until": f"{due:%d %b %H:%M}"})


async def escalate(case: dict, reason: str) -> None:
    case["escalations"].append({"at": clock.now().isoformat(timespec="seconds"), "to": "District Agriculture Office (mock notification)", "reason": reason})
    log(case, "pending", f"ESCALATED to District Agriculture Office: {reason}")
    store.add_trace(case["id"], "decision", "escalate_to_human", "warn", None, None, {"reason": reason})
    next_action(case, "District office to review", "District Agriculture Office", 24)
    await say(case, "escalated", {"reason": reason, "who": "district agriculture office", "next": "an officer will review your case; keep your photos and receipt ready"})


# ------------------------------------------------------------------ background tick

async def tick() -> None:
    """Runs every few seconds for all open cases; this is what keeps a case alive for days."""
    for case in store.list_cases(open_only=True):
        if case.get("demo") or case.get("scenario"):
            continue  # demo cases are display data; test-scenario cases are driven by their own run
        async with _locks[case["phone"]]:
            fresh = store.get_case(case["id"])
            changed = await tick_case(fresh)
            if changed:
                store.save_case(fresh)


async def tick_case(case: dict) -> bool:
    now = clock.now()
    h = hours_left(case)
    st = case["intimation"]["status"]

    # Pending portal submission: retry when due.
    if case["stage"] == "filing" and st == "pending" and clock.parse(case.get("next_retry")) and now >= clock.parse(case["next_retry"]):
        store.add_trace(case["id"], "decision", "background_retry", "ok", None, {"hours_left": round(h or 0, 1)}, None)
        if await tools.portal_health(case["id"]) and await try_portal(case):
            return True
        case["next_retry"] = (now + timedelta(seconds=30)).isoformat()
        return True

    # Docket received but not yet verified.
    if st == "submitted_unverified" and clock.parse(case.get("next_verify")) and now >= clock.parse(case["next_verify"]):
        await verify(case, case["intimation"]["reference"], case["intimation"]["route"])
        return True

    # Officer/bank hand-off: confirm forwarding or escalate when the window closes.
    ho = case.get("handoff")
    if ho and ho.get("report_id") and not ho.get("confirmed") and not ho.get("closed"):
        try:
            rs = await tools.officer_report_status(case["id"], ho["report_id"])
        except tools.ToolError:
            rs = {}
        if rs.get("forwarded"):
            ho["confirmed"] = True
            await verify(case, rs["docket_no"], ho["route"])
            return True
        if now >= clock.parse(ho["due"]) and not ho.get("escalated"):
            ho["escalated"] = True
            await escalate(case, f"Report {ho['report_id']} not forwarded by {routes.ROUTES[ho['route']]['label']} within the hand-off window")
            return True

    # Window closing with no verified report.
    if h is not None and st != "verified":
        if h <= 0 and not case.get("expired_escalated"):
            case["expired_escalated"] = True
            await escalate(case, "72-hour window closed without a verified intimation")
            return True
        for mark in (24, 6):
            key = f"reminded_{mark}"
            if h <= mark and not case.get(key) and case["stage"] in ("intake", "confirm"):
                case[key] = True
                await say(case, "deadline_reminder", {"hours_left": deadline.describe(h), "waiting_for": case["pending_question"]})
                return True

    # Tracking: surveyor assignment.
    if case["stage"] == "tracking" and not case.get("surveyor"):
        last = clock.parse(case.get("last_status_check"))
        if not last or now - last >= timedelta(minutes=1):
            case["last_status_check"] = now.isoformat()
            try:
                s = await tools.intimation_status(case["id"], case["intimation"]["reference"])
            except tools.ToolError:
                return True
            if s.get("surveyor"):
                case["surveyor"] = s["surveyor"]
                log(case, "retrieved", f"Surveyor assigned, visit on {s['surveyor']['visit_date']}")
                next_action(case, f"Surveyor visit on {s['surveyor']['visit_date']}; farmer confirms visit", "Insurer surveyor", 48)
                await say(case, "surveyor_assigned", {"visit_date": s["surveyor"]["visit_date"], "tip": "keep the damaged crop as it is until the visit"}, sms=True)
            return True
    return False
