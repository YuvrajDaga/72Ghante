"""Admin API: paged case list, case CRUD, consent, documents, status changes, farmers, scenarios, settings."""

from collections import defaultdict

from fastapi import APIRouter, File, HTTPException, UploadFile

from . import casesvc, clock, config, store, workflow

router = APIRouter(prefix="/api")


def _get(case_id: str) -> dict:
    case = store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return case


def _guard(fn, *args):
    try:
        return fn(*args)
    except casesvc.CaseError as err:
        raise HTTPException(422, str(err)) from err


def source_of(c: dict) -> str:
    """live = real conversations; test = deterministic simulation runs; demo = seeded synthetic records."""
    return "demo" if c.get("demo") else "test" if c.get("scenario") else "live"


def summary(c: dict) -> dict:
    dl = clock.parse(c.get("deadline"))
    f = c["fields"]
    return {
        "id": c["id"], "demo": bool(c.get("demo")), "scenario": c.get("scenario"), "source": source_of(c), "avatar": c.get("avatar", 0),
        "farmer": c.get("farmer_name") or "Farmer " + store.mask_phone(c["phone"])[-4:], "phone": store.mask_phone(c["phone"]),
        "crop": f.get("crop"), "peril": f.get("peril"), "district": f.get("district"), "taluka": f.get("taluka") or f.get("village"),
        "stage": c["stage"], "status": c["intimation"]["status"], "docket": c["intimation"]["reference"], "channel": c["channel"],
        "language": c.get("language"), "created": c.get("created"), "deadline": c.get("deadline"),
        "hours_left": round((dl - clock.now()).total_seconds() / 3600, 2) if dl else None,
    }


def _status_key(s: dict) -> str:
    if s["stage"] == "out_of_scope":
        return "area_based"
    if s["stage"] == "escalated":
        return "escalated"
    # Submission, acknowledgement and claim approval are different things; approval is never shown.
    if s["status"] == "verified":
        return "closed" if s["stage"] == "closed" else "acknowledged"
    if s["status"] == "submitted_unverified":
        return "submitted"
    if s["status"] == "pending":
        return "pending"
    if s["hours_left"] is not None and 0 < s["hours_left"] < 12:
        return "at_risk"
    if s["stage"] == "confirm":
        return "awaiting_info"
    return "in_progress"


@router.get("/cases")
async def list_cases(q: str = "", status: str = "", crop: str = "", district: str = "", source: str = "all",
                     sort: str = "updated", order: str = "desc", page: int = 1, size: int = 25, limit: int | None = None, live_only: bool = False):
    items = store.list_cases()
    if live_only:
        source = "live"
    if source == "all":
        items = [c for c in items if source_of(c) != "test"]  # test runs live on the Reports page
    elif source in ("live", "demo", "test"):
        items = [c for c in items if source_of(c) == source]
    # The simulated phone starts a new case on every "New chat": list only the newest case per demo phone, so one farmer shows once.
    seen: set[str] = set()
    kept = []
    for c in items:  # newest first
        if c.get("channel") == "sim":
            if not c.get("phone") or c["phone"] in seen:
                continue
            seen.add(c["phone"])
        elif c.get("channel") == "whatsapp" and not c.get("phone"):
            continue  # empty test chat with no farmer
        kept.append(c)
    items = kept
    rows = [summary(c) for c in items]
    for r in rows:
        r["status_key"] = _status_key(r)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in " ".join(str(v) for v in r.values()).lower()]
    if status:
        rows = [r for r in rows if r["status_key"] == status]
    if crop:
        rows = [r for r in rows if r["crop"] == crop]
    if district:
        rows = [r for r in rows if r["district"] == district]
    if sort != "updated":
        keyf = {"time_left": lambda r: (r["hours_left"] is None, r["hours_left"] or 0), "id": lambda r: r["id"],
                "farmer": lambda r: r["farmer"], "crop": lambda r: r["crop"] or "", "location": lambda r: r["taluka"] or "",
                "status": lambda r: r["status_key"], "created": lambda r: r["created"] or ""}.get(sort)
        if keyf:
            rows.sort(key=keyf, reverse=(order == "desc"))
    else:
        rows.sort(key=lambda r: r["source"] != "live")  # real cases first, then most recently updated
    if limit is not None:  # simple mode used by the dashboard widgets
        return rows[:limit]
    total = len(rows)
    size = max(1, min(size, 200))
    return {"total": total, "page": page, "size": size, "items": rows[(page - 1) * size: page * size]}


@router.get("/cases/{case_id}")
async def case_detail(case_id: str):
    case = _get(case_id)
    return {"case": case, "messages": store.messages_for_case(case_id), "trace": store.trace_for(case_id),
            "milestones": workflow.milestones(case), "stage_label": workflow.LABELS.get(case["stage"]),
            "allowed_transitions": sorted(t for t in workflow.TRANSITIONS.get(case["stage"], set()) if not workflow.check_transition(case, t))}


@router.post("/cases")
async def create(body: dict):
    try:
        case = await casesvc.create_case(body)
    except casesvc.CaseError as err:
        raise HTTPException(422, str(err)) from err
    return {"case_id": case["id"]}


@router.patch("/cases/{case_id}")
async def edit(case_id: str, body: dict):
    case = _guard(casesvc.edit_case, _get(case_id), body)
    return {"case_id": case["id"], "stage": case["stage"]}


@router.post("/cases/{case_id}/consent")
async def consent(case_id: str, body: dict):
    if body.get("confirmed") is not True:
        raise HTTPException(422, "Consent must be explicitly confirmed")
    _guard(casesvc.record_consent, _get(case_id), body.get("by") or "farmer, recorded by admin")
    return {"ok": True}


@router.post("/cases/{case_id}/submit")
async def submit(case_id: str):
    try:
        case = await casesvc.submit(_get(case_id))
    except casesvc.CaseError as err:
        raise HTTPException(422, str(err)) from err
    return {"status": case["intimation"]["status"], "docket": case["intimation"]["reference"]}


@router.post("/cases/{case_id}/transition")
async def transition(case_id: str, body: dict):
    case = _guard(casesvc.transition, _get(case_id), body.get("to", ""), body.get("reason", ""))
    return {"stage": case["stage"]}


@router.post("/cases/{case_id}/documents")
async def upload(case_id: str, file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10 MB)")
    case = await casesvc.add_document(_get(case_id), data, file.content_type or "application/octet-stream", file.filename or "upload")
    return {"evidence": case["evidence"][-1]}


@router.get("/farmers")
async def farmers(q: str = ""):
    groups: dict[str, dict] = defaultdict(lambda: {"cases": 0, "verified": 0, "open": 0, "crops": set(), "last": ""})
    meta = {}
    for c in store.list_cases():
        if c.get("scenario"):  # test-run farmers are synthetic; keep them off the Farmers page
            continue
        key = c["phone"]
        g = groups[key]
        g["cases"] += 1
        g["verified"] += c["intimation"]["status"] == "verified"
        g["open"] += c["stage"] not in ("closed", "out_of_scope")
        if c["fields"].get("crop"):
            g["crops"].add(c["fields"]["crop"])
        g["last"] = max(g["last"], c.get("created") or "")
        meta[key] = {"name": c.get("farmer_name") or "Unknown", "phone": store.mask_phone(c["phone"]), "district": c["fields"].get("district"),
                     "taluka": c["fields"].get("taluka") or c["fields"].get("village"), "demo": bool(c.get("demo")), "language": c.get("language"),
                     "latest_case": c["id"], "application_no": c["fields"].get("application_no"), "village": c["fields"].get("village"),
                     "consent": bool(c.get("consent_at")) or c["intimation"]["status"] == "verified"}
    out = [{**meta[k], **{**g, "crops": sorted(g["crops"])}} for k, g in groups.items()]
    if q:
        ql = q.lower()
        out = [f for f in out if ql in " ".join(str(v) for v in f.values()).lower()]
    out.sort(key=lambda f: f["last"], reverse=True)
    return out


@router.post("/scenarios/{kind}")
async def scenario(kind: str):
    try:
        run_id = await casesvc.run_scenario(kind)
    except casesvc.CaseError as err:
        raise HTTPException(404, str(err)) from err
    return {"run_id": run_id}


@router.get("/scenarios")
async def scenarios():
    return {"available": casesvc.SCENARIOS, "runs": store.list_runs()}


@router.get("/settings")
async def settings():
    """Configuration status only. Secrets are never returned."""
    return {
        "gemini_configured": bool(config.GEMINI_API_KEY), "gemini_model": config.GEMINI_MODEL, "fallback_models": config.GEMINI_FALLBACK_MODELS,
        "twilio_configured": bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN), "public_url_configured": bool(config.PUBLIC_BASE_URL),
        "pmfby_base_url": config.PMFBY_BASE_URL, "pmfby_is_mock": "/mock" in config.PMFBY_BASE_URL,
        "intimation_window_hours": config.INTIMATION_WINDOW_HOURS, "handoff_window_hours": config.HANDOFF_WINDOW_HOURS,
    }
