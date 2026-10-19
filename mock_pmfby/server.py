"""MOCK of the PMFBY systems the agent talks to. Synthetic data only.

We have no access to the real PMFBY APIs, so this mock copies the behaviour that matters
to the agent: policy lookup by mobile number, loss-intimation submission that returns a
docket number, status lookup, duplicate detection, the bank/officer report route, and
surveyor assignment. It also injects faults on demand (down, slow, flaky) so failure
recovery can be demonstrated and measured.

The real integration would replace only these HTTP calls (insurer / PMFBY partner API);
the agent does not change.
"""

import asyncio
import json
import random
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import os
from pathlib import Path

from app import clock
from app.config import DATA_DIR

mock = FastAPI(title="PMFBY MOCK (synthetic)")

SURVEYOR_DELAY_HOURS = 6
REGISTRY_FILE = Path(os.getenv("PMFBY_REGISTRY") or DATA_DIR / "mock_registry.json")  # tests use a temp file

# Synthetic farmers. Mobile numbers are fake; the dashboard can link a real demo phone to a profile.
PROFILES = {
    "sunita": {
        "name": "Sunita Pawar", "district": "Nashik", "taluka": "Niphad", "village": "Lasalgaon",
        "policies": [{
            "application_no": "0402-2026-K-118734", "insurer": "Agriculture Insurance Company of India", "season": "Kharif 2026",
            "bank": "Bank of Maharashtra, Lasalgaon", "plots": [{"survey_no": "112/2", "crop": "onion", "area_acres": 2.0}],
        }],
    },
    "ramesh": {
        "name": "Ramesh Shinde", "district": "Nashik", "taluka": "Dindori", "village": "Mohadi",
        "policies": [{
            "application_no": "0402-2026-K-120551", "insurer": "Agriculture Insurance Company of India", "season": "Kharif 2026",
            "bank": "Nashik DCC Bank, Dindori", "plots": [
                {"survey_no": "45/1", "crop": "soybean", "area_acres": 1.5},
                {"survey_no": "78/3", "crop": "maize", "area_acres": 2.0},
            ],
        }],
    },
    "bhagirath": {
        "name": "Bhagirath Choudhary", "district": "Jodhpur", "taluka": "Osian", "village": "Tinwari",
        "policies": [{
            "application_no": "0815-2026-K-077310", "insurer": "Agriculture Insurance Company of India", "season": "Kharif 2026",
            "bank": "SBI, Osian", "plots": [{"survey_no": "233", "crop": "bajra", "area_acres": 4.0}],
        }],
    },
}
DEFAULT_LINKS = {"9000000001": "sunita", "9000000002": "ramesh", "9000000003": "bhagirath"}

state = {
    "faults": {"submit": "ok", "status": "ok", "lookup": "ok"},
    "links": dict(DEFAULT_LINKS),
    "intimations": {},  # docket -> record
    "reports": {},      # officer/bank report id -> record
    "calls": 0,
}


def _load() -> None:
    if REGISTRY_FILE.exists():
        saved = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        for key in ("links", "intimations", "reports"):
            state[key].update(saved.get(key, {}))


def _save() -> None:
    REGISTRY_FILE.write_text(json.dumps({k: state[k] for k in ("links", "intimations", "reports")}, ensure_ascii=False, indent=1), encoding="utf-8")


_load()


def _digits(mobile: str) -> str:
    return "".join(c for c in mobile if c.isdigit())[-10:]


async def _behave(endpoint: str) -> None:
    """Realistic latency plus whatever fault is switched on for this endpoint."""
    state["calls"] += 1
    mode = state["faults"].get(endpoint, "ok")
    if mode == "down":
        raise HTTPException(503, "Service Unavailable")
    if mode == "slow":
        await asyncio.sleep(20)
    if mode == "flaky" and random.random() < 0.5:
        raise HTTPException(random.choice([500, 502, 504]), "Upstream error")
    await asyncio.sleep(random.uniform(0.2, 0.8))


def _profile_for(mobile: str) -> dict | None:
    key = state["links"].get(_digits(mobile))
    return PROFILES.get(key) if key else None


@mock.get("/health")
async def health():
    return {"ok": True, "faults": state["faults"]}


@mock.post("/api/v1/policy/lookup")
async def policy_lookup(body: dict):
    await _behave("lookup")
    profile = _profile_for(body.get("mobile", ""))
    if not profile:
        return {"found": False}
    return {"found": True, "farmer": profile}


def _find_policy(application_no: str) -> tuple[dict, dict] | None:
    for profile in PROFILES.values():
        for policy in profile["policies"]:
            if policy["application_no"] == application_no:
                return profile, policy
    return None


@mock.post("/api/v1/intimation")
async def submit_intimation(body: dict):
    await _behave("submit")
    found = _find_policy(body.get("application_no", ""))
    if not found:
        return JSONResponse({"error": "UNKNOWN_APPLICATION", "message": "Application number not found"}, 422)
    profile, policy = found
    if body.get("survey_no") not in [p["survey_no"] for p in policy["plots"]]:
        return JSONResponse({"error": "SURVEY_MISMATCH", "message": "Survey number not insured under this application"}, 422)
    event = clock.parse(body.get("event_time"))
    if not event:
        return JSONResponse({"error": "BAD_EVENT_TIME"}, 422)
    late = clock.now() - event > timedelta(hours=72)
    # Duplicate detection: same application, plot and event day.
    for docket, rec in state["intimations"].items():
        if rec["application_no"] == body["application_no"] and rec["survey_no"] == body["survey_no"] and rec["event_time"][:10] == body["event_time"][:10]:
            return JSONResponse({"error": "DUPLICATE", "docket_no": docket, "received_at": rec["received_at"]}, 409)
    state_code = "RJ" if profile["district"] == "Jodhpur" else "MH"
    docket = f"PMFBY-{state_code}-{clock.now():%y%m}-{random.randint(100000, 999999)}"
    state["intimations"][docket] = {
        **{k: body.get(k) for k in ("application_no", "survey_no", "crop", "peril", "event_time", "reported_via", "photos")},
        "received_at": clock.now().isoformat(timespec="seconds"), "late": late, "surveyor": None,
    }
    _save()
    return JSONResponse({"docket_no": docket, "received_at": state["intimations"][docket]["received_at"], "late": late}, 201)


@mock.get("/api/v1/intimation/{docket}")
async def intimation_status(docket: str):
    await _behave("status")
    rec = state["intimations"].get(docket)
    if not rec:
        raise HTTPException(404, "Docket not found")
    received = clock.parse(rec["received_at"])
    if not rec["surveyor"] and clock.now() - received >= timedelta(hours=SURVEYOR_DELAY_HOURS):
        visit = clock.now() + timedelta(days=2)
        rec["surveyor"] = {"name": "Field Surveyor (mock)", "visit_date": f"{visit:%Y-%m-%d}"}
        _save()
    status = "SURVEYOR_ASSIGNED" if rec["surveyor"] else "RECEIVED"
    return {"docket_no": docket, "status": status, "received_at": rec["received_at"], "surveyor": rec["surveyor"], "late": rec["late"]}


@mock.post("/api/v1/officer/report")
async def officer_report(body: dict):
    """A report handed to a bank branch or agriculture officer, who must forward it onward."""
    await _behave("lookup")
    report_id = f"RPT-{random.randint(10000, 99999)}"
    state["reports"][report_id] = {"body": body, "created": clock.now().isoformat(timespec="seconds"), "forwarded_docket": None}
    _save()
    return {"report_id": report_id}


@mock.get("/api/v1/officer/report/{report_id}")
async def officer_report_status(report_id: str):
    rec = state["reports"].get(report_id)
    if not rec:
        raise HTTPException(404, "Report not found")
    return {"report_id": report_id, "forwarded": bool(rec["forwarded_docket"]), "docket_no": rec["forwarded_docket"]}


# ---- Admin controls used by the dashboard (demo + evaluation) ----

@mock.get("/admin/state")
async def admin_state():
    return {"faults": state["faults"], "links": state["links"], "intimations": state["intimations"], "reports": state["reports"], "profiles": list(PROFILES)}


@mock.post("/admin/faults")
async def admin_faults(body: dict):
    for endpoint, mode in body.items():
        if endpoint in state["faults"] and mode in ("ok", "down", "slow", "flaky"):
            state["faults"][endpoint] = mode
    return state["faults"]


@mock.post("/admin/link")
async def admin_link(body: dict):
    """Link a real demo phone number to a synthetic profile."""
    profile = body.get("profile")
    if profile not in PROFILES:
        raise HTTPException(400, f"profile must be one of {list(PROFILES)}")
    state["links"][_digits(body.get("mobile", ""))] = profile
    _save()
    return {"linked": True}


@mock.post("/admin/register_policy")
async def admin_register_policy(body: dict):
    """Adds a synthetic policy (used by deterministic test scenarios and admin-created cases)."""
    key = "sim_" + body["application_no"]
    PROFILES[key] = {
        "name": body.get("name", "Synthetic Farmer"), "district": body.get("district", "Jodhpur"),
        "taluka": body.get("taluka", ""), "village": body.get("village", ""),
        "policies": [{"application_no": body["application_no"], "insurer": "Agriculture Insurance Company of India (synthetic)",
                      "season": "Kharif 2026", "bank": body.get("bank", "SBI (synthetic)"),
                      "plots": [{"survey_no": body["survey_no"], "crop": body.get("crop", "bajra"), "area_acres": body.get("area_acres", 2.0)}]}],
    }
    if body.get("mobile"):
        state["links"][_digits(body["mobile"])] = key
    return {"registered": key}


@mock.post("/admin/officer_forward")
async def admin_officer_forward(body: dict):
    """Simulate the officer forwarding a report onward (they then get a docket)."""
    rec = state["reports"].get(body.get("report_id", ""))
    if not rec:
        raise HTTPException(404, "Report not found")
    payload = {**rec["body"], "reported_via": "officer"}
    found = _find_policy(payload.get("application_no", ""))
    if not found:
        raise HTTPException(422, "Unknown application")
    profile, _ = found
    state_code = "RJ" if profile["district"] == "Jodhpur" else "MH"
    docket = f"PMFBY-{state_code}-{clock.now():%y%m}-{random.randint(100000, 999999)}"
    state["intimations"][docket] = {
        **{k: payload.get(k) for k in ("application_no", "survey_no", "crop", "peril", "event_time", "reported_via", "photos")},
        "received_at": clock.now().isoformat(timespec="seconds"), "late": False, "surveyor": None,
    }
    rec["forwarded_docket"] = docket
    _save()
    return {"docket_no": docket}


@mock.post("/admin/reset")
async def admin_reset():
    state["faults"] = {"submit": "ok", "status": "ok", "lookup": "ok"}
    state["links"] = dict(DEFAULT_LINKS)
    state["intimations"].clear()
    state["reports"].clear()
    _save()
    return {"reset": True}
