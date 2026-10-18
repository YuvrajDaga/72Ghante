"""Case service: create, edit, consent, documents and status changes for admin-created cases,
plus the deterministic test scenarios behind the dashboard's Simulation buttons.

Deadline arithmetic and status rules are plain code (deadline.py, workflow.py). No model call
happens here; replies on these cases use fixed templates.
"""

import asyncio
import io
import itertools
from datetime import timedelta

from PIL import Image, ImageFilter

from mock_pmfby import server as mock_server

from . import agent, channels, clock, deadline, store, tools, workflow

EDITABLE = {"farmer_name", "district", "taluka", "village", "crop", "peril", "crop_harvested", "survey_no", "application_no", "area_acres"}
REQUIRED = ["peril", "crop", "survey_no", "application_no"]


class CaseError(ValueError):
    pass


def _recompute(case: dict) -> None:
    f = case["fields"]
    case["peril_class"] = deadline.classify_peril(f.get("peril"), f.get("crop_harvested"))
    if case.get("event_time"):
        ev = clock.parse(case["event_time"])
        case["deadline"] = deadline.deadline_for(ev).isoformat()


async def create_case(data: dict, scripted: bool = True) -> dict:
    phone = (data.get("phone") or "").strip()
    if len("".join(c for c in phone if c.isdigit())) < 10:
        raise CaseError("Phone number must have at least 10 digits")
    ev = clock.parse(data.get("event_time"))
    if not ev:
        raise CaseError("Incident date and time are required")
    if ev > clock.now() + timedelta(minutes=5):
        raise CaseError("Incident time cannot be in the future")
    if data.get("peril") and data["peril"] not in deadline.LOCALISED | deadline.POST_HARVEST | deadline.WIDESPREAD:
        raise CaseError(f"Unknown loss type '{data['peril']}'")

    case = store.new_case(phone, data.get("channel", "manual"))
    case["scripted"] = scripted
    case["farmer_name"] = data.get("farmer_name") or None
    case["language"] = data.get("language", "hi")
    for k in EDITABLE:
        if data.get(k) not in (None, ""):
            case["fields"][k] = data[k]
            case["sources"][k] = "admin"
    case["fields"]["event_time"] = ev.isoformat()
    case["event_time"] = ev.isoformat()
    _recompute(case)
    h = deadline.hours_left(clock.parse(case["deadline"]))
    agent.log(case, "known", f"Case created by admin. 72-hour deadline {case['deadline'][:16]} ({deadline.describe(h)})")
    store.add_trace(case["id"], "decision", "start_clock", "ok", None, {"event_time": case["event_time"]}, {"deadline": case["deadline"], "hours_left": round(h, 2), "tier": deadline.urgency(h)})

    # Synthetic policy in the MOCK registry so the mock portal can accept the filing.
    if case["fields"].get("application_no") and case["fields"].get("survey_no"):
        await mock_server.admin_register_policy({
            "application_no": case["fields"]["application_no"], "survey_no": case["fields"]["survey_no"],
            "crop": case["fields"].get("crop"), "district": case["fields"].get("district", ""), "name": case["farmer_name"] or "", "mobile": phone})

    if case["peril_class"] == "widespread":
        case["stage"] = "out_of_scope"
        agent.log(case, "done", "Widespread loss: area-based assessment, no individual 72-hour intimation")
    elif all(case["fields"].get(k) for k in REQUIRED):
        case["stage"] = "confirm"
        case["awaiting_consent"] = True
        case["info_complete_at"] = clock.now().isoformat(timespec="seconds")
        agent.log(case, "known", "All required information present; waiting for farmer consent")
    store.save_case(case)
    return case


def edit_case(case: dict, changes: dict) -> dict:
    if case["stage"] in ("filing", "tracking", "closed"):
        raise CaseError("A case that is being filed or already acknowledged cannot be edited")
    unknown = set(changes) - EDITABLE - {"event_time"}
    if unknown:
        raise CaseError(f"Not editable: {', '.join(sorted(unknown))}")
    for k, v in changes.items():
        if k == "event_time":
            ev = clock.parse(v)
            if not ev or ev > clock.now() + timedelta(minutes=5):
                raise CaseError("Invalid incident time")
            case["event_time"] = case["fields"]["event_time"] = ev.isoformat()
        elif k == "farmer_name":
            case["farmer_name"] = v
        else:
            case["fields"][k] = v
            case["sources"][k] = "admin"
    _recompute(case)
    # Any edit invalidates an earlier consent: the farmer must confirm the corrected details.
    if case.get("consent_at"):
        case["consent_at"] = None
        agent.log(case, "pending", "Details edited after consent; consent cleared and must be given again")
    if case["stage"] == "intake" and all(case["fields"].get(k) for k in REQUIRED):
        case["stage"] = "confirm"
        case["awaiting_consent"] = True
        case["info_complete_at"] = clock.now().isoformat(timespec="seconds")
    agent.log(case, "known", f"Edited by admin: {', '.join(sorted(changes))}")
    store.save_case(case)
    return case


def record_consent(case: dict, by: str) -> dict:
    if case["stage"] != "confirm":
        raise CaseError("Consent can only be recorded while the case is awaiting consent")
    case["consent_at"] = clock.now().isoformat(timespec="seconds")
    case["consent_by"] = by
    case["awaiting_consent"] = False
    agent.log(case, "known", f"Explicit consent recorded ({by})")
    store.add_trace(case["id"], "decision", "consent_received", "ok", None, {"by": by}, {"at": case["consent_at"]})
    store.save_case(case)
    return case


async def submit(case: dict) -> dict:
    err = workflow.check_transition(case, "filing")
    if err:
        raise CaseError(err)
    case["stage"] = "filing"
    await agent.file_case(case)
    store.save_case(case)
    return case


def transition(case: dict, to: str, reason: str) -> dict:
    err = workflow.check_transition(case, to)
    if err:
        raise CaseError(err)
    agent.log(case, "known", f"Status changed {case['stage']} → {to} by admin: {reason or 'no reason given'}")
    case["stage"] = to
    store.save_case(case)
    return case


async def add_document(case: dict, data: bytes, mime: str, filename: str) -> dict:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()[:5]
    path = channels.save_media(data, ext)
    if mime.startswith("image"):
        await agent.handle_image(case, data, mime, path, use_vision=not case.get("scripted"))
        case.pop("_reply_override", None)
        case["evidence"][-1].update({"filename": filename, "bytes": len(data), "mime": mime})
    else:
        case["evidence"].append({"type": "document", "path": path, "accepted": True, "reason": "uploaded by admin",
                                 "filename": filename, "bytes": len(data), "mime": mime, "received_at": clock.now().isoformat(timespec="seconds")})
        agent.log(case, "retrieved", f"Document uploaded: {filename}")
    store.save_case(case)
    return case


# ------------------------------------------------------------------ deterministic test scenarios

_seq = itertools.count(1)
SCENARIOS = {
    "success": "Portal healthy: file, verify, acknowledge",
    "timeout": "Portal times out on every attempt, then recovers: retries, pending-not-done, fallback, recovery",
    "server_failure": "Portal returns 503, then recovers: pending-not-done, helpline fallback, background recovery",
    "invalid_document": "Damage photo is too dark and blurry: rejected with a reason, case not filed",
    "hour61": "Loss reported at hour 61 of 72, then the portal breaks: fast fallback before the window closes",
}


def _bad_photo() -> bytes:
    img = Image.new("RGB", (800, 600), (18, 22, 16)).filter(ImageFilter.GaussianBlur(8))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


async def run_scenario(kind: str) -> int:
    if kind not in SCENARIOS:
        raise CaseError(f"Unknown scenario '{kind}'")
    run_id = store.start_run(kind)
    asyncio.create_task(_run(run_id, kind))
    return run_id


async def _run(run_id: int, kind: str) -> None:
    n = next(_seq)
    case = None
    try:
        await mock_server.admin_faults({"submit": "ok", "status": "ok", "lookup": "ok"})
        stamp = clock.now().strftime("%H%M%S")
        case = await create_case({
            "phone": f"+9180000{run_id:05d}", "farmer_name": f"Test Farmer {run_id} (synthetic)", "district": "Jodhpur", "taluka": "Osian",
            "crop": "bajra", "peril": "hailstorm", "survey_no": f"T{run_id}/{n}", "application_no": f"SIM-2026-{stamp}{run_id:03d}",
            "event_time": (clock.now() - timedelta(hours=61 if kind == "hour61" else 20)).isoformat(), "channel": "test"})
        case["scenario"] = kind
        store.save_case(case)
        store.set_run_case(run_id, case["id"])
        steps = [f"created {case['id']} (deadline {case['deadline'][:16]})"]

        if kind == "invalid_document":
            await add_document(case, _bad_photo(), "image/jpeg", "dark_blurry.jpg")
            ev = case["evidence"][-1]
            ok = not ev["accepted"]
            steps.append(f"photo {'rejected' if ok else 'ACCEPTED'}: {ev['reason']}")
            store.finish_run(run_id, case["id"], "passed" if ok else "failed", "Bad photo rejected with a reason; nothing filed" if ok else "Bad photo was accepted", steps)
            return

        record_consent(case, "test scenario")
        steps.append("consent recorded")
        if kind == "timeout":
            await mock_server.admin_faults({"submit": "slow"})
            tools.SUBMIT_TIMEOUT_S = 2  # keep the demo run short; production default is 8 s
        elif kind in ("server_failure", "hour61"):
            await mock_server.admin_faults({"submit": "down"})
        try:
            await submit(case)
        finally:
            tools.SUBMIT_TIMEOUT_S = 8
        steps.append(f"after first filing: status={case['intimation']['status']}, attempts={len(case['attempts'])}")

        if kind != "success":
            if case["intimation"]["status"] == "verified":
                store.finish_run(run_id, case["id"], "failed", "Expected a failure but filing succeeded", steps)
                return
            await mock_server.admin_faults({"submit": "ok"})
            steps.append("portal restored")
            for _ in range(6):  # background retry loop, driven directly so the run is deterministic
                case["next_retry"] = clock.now().isoformat()
                await agent.tick_case(case)
                store.save_case(case)
                if case["intimation"]["status"] == "verified":
                    break
                await asyncio.sleep(1)
            steps.append(f"after recovery: status={case['intimation']['status']}")

        ok = case["intimation"]["status"] == "verified" and bool(case["intimation"]["reference"])
        retries = sum(1 for a in case["attempts"] if a["result"] == "failed")
        outcome = (f"Verified docket {case['intimation']['reference']} after {retries} failed attempt(s)" if ok else "Not verified")
        store.finish_run(run_id, case["id"], "passed" if ok else "failed", outcome, steps)
    except Exception as err:  # noqa: BLE001 - a broken run is reported, never hidden
        store.finish_run(run_id, case["id"] if case else None, "error", f"{type(err).__name__}: {err}", None)
    finally:
        await mock_server.admin_faults({"submit": "ok", "status": "ok", "lookup": "ok"})
        if case:
            store.save_case(case)
