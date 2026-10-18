"""Core tests: deadline maths, status rules, duplicate filing, timeout recovery, simulation scenarios.

Runs a private server on port 8765 with a temporary database, so the real data is never touched.
No Gemini calls: every case here is "scripted" (fixed reply templates).
"""

import asyncio
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

import pytest

os.environ["GHANTE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["PMFBY_BASE_URL"] = "http://127.0.0.1:8765/mock"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import agent, casesvc, clock, deadline, store, tools, workflow  # noqa: E402
from app.main import app  # noqa: E402
from mock_pmfby import server as mock_server  # noqa: E402

IST = clock.IST


@pytest.fixture(scope="session", autouse=True)
def server():
    cfg = uvicorn.Config(app, port=8765, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:8765/mock/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield
    srv.should_exit = True


def run(coro):
    return asyncio.run(coro)


# ---------------- deadline maths

def test_deadline_is_72_hours_after_event():
    ev = datetime(2026, 10, 17, 22, 0, tzinfo=IST)
    assert deadline.deadline_for(ev) == datetime(2026, 10, 20, 22, 0, tzinfo=IST)


@pytest.mark.parametrize("hours,tier", [(50, "normal"), (24.5, "normal"), (23.9, "hurry"), (6, "hurry"), (5.9, "critical"), (0, "expired"), (-3, "expired")])
def test_urgency_tiers(hours, tier):
    assert deadline.urgency(hours) == tier


@pytest.mark.parametrize("peril,harvested,regime", [
    ("hailstorm", None, "localised"), ("unseasonal_rain", True, "post_harvest"), ("unseasonal_rain", False, "widespread"),
    ("unseasonal_rain", None, "needs_info"), ("drought", None, "widespread"), (None, None, "unknown")])
def test_peril_regime(peril, harvested, regime):
    assert deadline.classify_peril(peril, harvested) == regime


# ---------------- status transitions

def _case(stage, consent=None, status="not_started"):
    return {"stage": stage, "consent_at": consent, "intimation": {"status": status}}


def test_filing_requires_consent():
    assert workflow.check_transition(_case("confirm"), "filing") == "Explicit farmer consent is required before filing"
    assert workflow.check_transition(_case("confirm", consent="2026-10-18T10:00"), "filing") is None


def test_tracking_requires_verified_docket():
    assert workflow.check_transition(_case("filing"), "tracking")
    assert workflow.check_transition(_case("filing", status="verified"), "tracking") is None


def test_illegal_jumps_rejected():
    assert workflow.check_transition(_case("intake"), "closed")
    assert workflow.check_transition(_case("closed"), "intake")
    assert workflow.check_transition(_case("intake"), "nonsense")


def test_create_case_validates_input():
    with pytest.raises(casesvc.CaseError):
        run(casesvc.create_case({"phone": "123", "event_time": clock.now().isoformat()}))
    with pytest.raises(casesvc.CaseError):
        run(casesvc.create_case({"phone": "+919999900001", "event_time": (clock.now() + timedelta(days=1)).isoformat()}))


def test_edit_after_consent_clears_consent():
    async def go():
        c = await casesvc.create_case(_new("+919999900002", "T-EDIT-1"))
        casesvc.record_consent(c, "test")
        casesvc.edit_case(c, {"survey_no": "99/9"})
        return c
    c = run(go())
    assert c["consent_at"] is None


# ---------------- filing behaviour against the mock portal

def _new(phone, app_no, hours_ago=10):
    return {"phone": phone, "farmer_name": "Test", "district": "Jodhpur", "crop": "bajra", "peril": "hailstorm",
            "survey_no": "1/1", "application_no": app_no, "event_time": (clock.now() - timedelta(hours=hours_ago)).isoformat()}


def test_success_files_and_verifies():
    async def go():
        await mock_server.admin_faults({"submit": "ok", "status": "ok"})
        c = await casesvc.create_case(_new("+919999900003", "T-OK-1"))
        casesvc.record_consent(c, "test")
        await casesvc.submit(c)
        return c
    c = run(go())
    assert c["intimation"]["status"] == "verified"
    assert c["intimation"]["reference"].startswith("PMFBY-")
    assert c["stage"] == "tracking"


def test_duplicate_submission_is_not_filed_twice():
    async def go():
        await mock_server.admin_faults({"submit": "ok", "status": "ok"})
        a = await casesvc.create_case(_new("+919999900004", "T-DUP-1"))
        casesvc.record_consent(a, "test")
        await casesvc.submit(a)
        b = await casesvc.create_case(_new("+919999900005", "T-DUP-1"))
        b["fields"]["survey_no"] = "1/1"
        casesvc.record_consent(b, "test")
        await casesvc.submit(b)
        return a, b
    a, b = run(go())
    assert b["intimation"]["reference"] == a["intimation"]["reference"]
    assert any(x["result"] == "duplicate" for x in b["attempts"])
    dockets = [d for d, rec in mock_server.state["intimations"].items() if rec["application_no"] == "T-DUP-1"]
    assert len(dockets) == 1


def test_timeout_never_claims_done_then_recovers():
    async def go():
        c = await casesvc.create_case(_new("+919999900006", "T-TO-1"))
        casesvc.record_consent(c, "test")
        await mock_server.admin_faults({"submit": "slow"})
        tools.SUBMIT_TIMEOUT_S = 1
        try:
            await casesvc.submit(c)
        finally:
            tools.SUBMIT_TIMEOUT_S = 8
        first = (c["intimation"]["status"], c["intimation"]["reference"], len(c["attempts"]))
        await mock_server.admin_faults({"submit": "ok"})
        c["next_retry"] = clock.now().isoformat()
        await agent.tick_case(c)
        return first, c
    first, c = run(go())
    assert first[0] == "pending" and first[1] is None  # no false "done"
    assert first[2] == agent.SUBMIT_RETRIES             # retry limit respected
    assert c["intimation"]["status"] == "verified"


@pytest.mark.parametrize("kind", ["success", "timeout", "server_failure", "invalid_document"])
def test_simulation_scenarios_pass(kind):
    async def go():
        run_id = store.start_run(kind)
        await casesvc._run(run_id, kind)
        return next(r for r in store.list_runs() if r["id"] == run_id)
    r = run(go())
    assert r["status"] == "passed", r
