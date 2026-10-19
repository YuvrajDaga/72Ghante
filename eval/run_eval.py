"""Real evaluation: 45 farmer conversations, our agent vs a plain chatbot (same AI, no tools, no rules).

Run:  .venv\\Scripts\\python -m eval.run_eval
Writes eval/results/summary.json, which the Model Evaluation page shows.

Fair setup
- Both systems get the same farmer messages (Hindi / Marathi / Marwadi) and the same AI models
  (Gemini, with the free Groq backup when Gemini is busy).
- Our agent: the real agent code with the real tools (mock PMFBY portal, policy lookup, receipt check).
  A simulated farmer answers whatever the agent asks, from a fixed fact sheet for the case.
- Plain chatbot: told to help the farmer report the loss; it gets the full story plus "yes, file it".
  It has no tools, so it cannot really file; we record what it CLAIMS.
- 12 cases break the portal (503 down / timeout) on the first try, then the portal recovers.
Scoring is automatic against each case's expected answer (no human judging).
Runs on a private temporary database, so the dashboard's data is never touched.
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from datetime import timedelta

TMP = tempfile.mkdtemp()
os.environ["GHANTE_DB"] = os.path.join(TMP, "eval.db")
os.environ["PMFBY_REGISTRY"] = os.path.join(TMP, "registry.json")
os.environ["PMFBY_BASE_URL"] = "http://127.0.0.1:8766/mock"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import agent, clock, llm, store  # noqa: E402
from app.config import ROOT  # noqa: E402
from app.main import app  # noqa: E402
from mock_pmfby import server as mock  # noqa: E402

PROFILES = {  # profile -> its insured crops
    "sunita": ["onion"], "ramesh": ["soybean", "maize"], "bhagirath": ["bajra"],
}
# Words per language
W = {
    "hi": {"hail": "ओले गिरे", "rain": "बेमौसम बारिश हुई", "flood": "खेत में पानी भर गया", "fire": "खेत में आग लग गई",
           "onion": "प्याज", "soybean": "सोयाबीन", "maize": "मक्का", "bajra": "बाजरा",
           "y22": "कल रात", "y14": "कल दोपहर", "d18": "परसों शाम",
           "open": "{when} {peril}, मेरी {crop} की फसल खराब हो गई।", "vague": "मेरी फसल का बहुत नुकसान हो गया, मदद करो।",
           "nophoto": "फोटो नहीं है, आगे बढ़िए", "yes": "हाँ, भेज दीजिए", "standing": "फसल खड़ी थी, अभी कटी नहीं थी", "harvested": "फसल कट चुकी थी, खेत में सूख रही थी"},
    "mr": {"hail": "गारपीट झाली", "rain": "अवकाळी पाऊस पडला", "flood": "शेतात पाणी भरलं", "fire": "शेताला आग लागली",
           "onion": "कांदा", "soybean": "सोयाबीन", "maize": "मका", "bajra": "बाजरी",
           "y22": "काल रात्री", "y14": "काल दुपारी", "d18": "परवा संध्याकाळी",
           "open": "{when} {peril}, माझ्या {crop} पिकाचं नुकसान झालं.", "vague": "माझ्या पिकाचं खूप नुकसान झालं, मदत करा.",
           "nophoto": "फोटो नाही, पुढे जा", "yes": "हो, पाठवा", "standing": "पीक उभं होतं, अजून काढलं नव्हतं", "harvested": "पीक काढून शेतात वाळत ठेवलं होतं"},
    "mwr": {"hail": "ओळा पड्या", "rain": "बेमौसम बरसात होई", "flood": "खेत में पाणी भरग्यो", "fire": "खेत में आग लागगी",
            "onion": "कांदो", "soybean": "सोयाबीन", "maize": "मक्की", "bajra": "बाजरी",
            "y22": "काल रात", "y14": "काल दोपहर", "d18": "परसूं सांझ",
            "open": "{when} {peril}, म्हारी {crop} री फसल खराब होगी।", "vague": "म्हारी फसल रो घणो नुकसान होयो, मदद करो।",
            "nophoto": "फोटू कोनी, आगे बढ़ो", "yes": "हां, भेज दो", "standing": "फसल खड़ी ही, कटी कोनी", "harvested": "फसल काट'र खेत में सूखण सारू राखी ही"},
}
PERIL_KEY = {"hail": "hailstorm", "rain": "unseasonal_rain", "flood": "inundation", "fire": "natural_fire"}
WHEN = {"y22": (1, 22), "y14": (1, 14), "d18": (2, 18)}  # days ago, hour


def build_cases() -> list[dict]:
    langs = ["hi"] * 21 + ["mr"] * 15 + ["mwr"] * 9
    perils, whens = ["hail", "rain", "flood", "fire"], ["y22", "y14", "d18"]
    cases = []
    for i, lang in enumerate(langs):
        prof = "bhagirath" if lang == "mwr" else ("sunita" if i % 2 == 0 else "ramesh")
        crop = PROFILES[prof][i % len(PROFILES[prof])]
        kind = ["normal", "normal", "vague", "fail_down", "normal", "wrong_crop", "fail_slow", "normal"][i % 8]
        if kind == "wrong_crop" and prof != "ramesh":
            kind = "normal"
        c = {"id": f"TC-{i + 1:03d}", "lang": lang, "profile": prof, "crop": crop, "peril": perils[i % 4], "when": whens[i % 3], "kind": kind}
        # PMFBY rule: rain on a STANDING crop is assessed area-wide (no individual 72h report); on a HARVESTED crop it is an individual claim.
        c["harvested"] = c["peril"] == "rain" and i % 8 != 1
        c["expect"] = "area_based" if c["peril"] == "rain" and not c["harvested"] else "verified"
        if c["expect"] == "area_based":
            c["kind"] = "area_based"
        cases.append(c)
    return cases


def farmer_answer(c: dict, pending: str | None, awaiting_consent: bool) -> str:
    w = W[c["lang"]]
    if awaiting_consent:
        return w["yes"]
    return {"peril": f"{w[c['when']]} {w[c['peril']]}", "event_time": w[c["when"]], "crop": w[c["crop"]],
            "plot_choice": w[c["crop"]], "photo": w["nophoto"], "crop_harvested": w["harvested"] if c.get("harvested") else w["standing"],
            "conflict:crop": w[c["crop"]]}.get(pending or "", f"{w[c['when']]} {w[c['peril']]}, {w[c['crop']]}")


def opening(c: dict) -> str:
    w = W[c["lang"]]
    if c["kind"] == "vague":
        return w["vague"]
    crop = w["onion"] if c["kind"] == "wrong_crop" else w[c["crop"]]  # ramesh says onion, which his policy does not cover
    return w["open"].format(when=w[c["when"]], peril=w[c["peril"]], crop=crop)


def expected_event(c: dict):
    days, hour = WHEN[c["when"]]
    return (clock.now() - timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def llm_calls(case_id: str) -> int:
    return store._db.execute("SELECT COUNT(*) FROM trace WHERE case_id=? AND kind='llm' AND status='ok'", (case_id,)).fetchone()[0]


async def run_ours(c: dict, n: int) -> dict:
    phone = f"+9171{n:08d}"
    mock.state["links"][phone[-10:]] = c["profile"]
    t0 = time.perf_counter()
    if c["kind"].startswith("fail"):
        await mock.admin_faults({"submit": "down" if c["kind"] == "fail_down" else "slow"})
    case = store.new_case(phone, "sim")
    case["scenario"] = "eval"  # the server's background loop leaves it alone; the eval drives it
    case["scripted"] = True  # replies from fixed templates: the evaluation measures understanding + decisions, not wording
    store.save_case(case)
    msg = opening(c)
    told_uninsured = False
    for _ in range(9):
        case = await agent.handle_inbound(phone, "sim", msg, [])
        if c["kind"] == "wrong_crop" and case.get("uninsured_crop"):
            told_uninsured = True
        if case["stage"] in ("filing", "tracking", "closed", "escalated", "out_of_scope"):
            break
        msg = farmer_answer(c, case.get("pending_question"), case.get("awaiting_consent"))
    failed_first = any(a["result"] == "failed" for a in case.get("attempts", []))
    if c["kind"].startswith("fail"):
        # The portal comes back; the background loop retries (we run one tick now instead of waiting).
        await mock.admin_faults({"submit": "ok"})
        for _ in range(3):
            case = store.get_case(case["id"])
            if case["intimation"]["status"] == "verified":
                break
            case["next_retry"] = clock.now().isoformat()
            if case["intimation"]["status"] == "submitted_unverified":
                case["next_verify"] = clock.now().isoformat()
            await agent.tick_case(case)
            store.save_case(case)
    case = store.get_case(case["id"])
    f, ev = case["fields"], clock.parse(case.get("event_time"))
    exp = expected_event(c)
    sent_done = [m for m in store.messages_for_case(case["id"]) if m["direction"] == "out" and ("Docket" in (m["text"] or "") or "verified" in (m["text"] or "").lower())]
    area = c["expect"] == "area_based"
    return {
        "completed": case["stage"] == "out_of_scope" if area else case["intimation"]["status"] == "verified",
        "deadline_ok": None if area else bool(case.get("deadline")) and abs((clock.parse(case["deadline"]) - (exp + timedelta(hours=72))).total_seconds()) <= 3 * 3600,
        "fields_ok": f.get("peril") == PERIL_KEY[c["peril"]] and f.get("crop") == c["crop"],
        "recovered": case["intimation"]["status"] == "verified" if failed_first else None,
        "false_done": bool(sent_done) and case["intimation"]["status"] != "verified",
        "told_uninsured": told_uninsured if c["kind"] == "wrong_crop" else None,
        "secs": round(time.perf_counter() - t0, 1), "calls": llm_calls(case["id"]),
        "docket": case["intimation"]["reference"], "status": case["intimation"]["status"],
    }


BASE_SYSTEM = """You are a helpful WhatsApp assistant for Indian farmers. Help the farmer report crop loss under PMFBY
(crop insurance). Reply in the farmer's language. Return JSON {"reply": str}."""
BASE_FINAL = """Based on the conversation, return JSON:
{"filed": true if you have filed/submitted/registered the loss report for the farmer, else false,
 "docket": the docket/reference number you gave the farmer, or null,
 "peril": one English label of hailstorm|unseasonal_rain|inundation|natural_fire|other,
 "crop": the insured crop in English (onion|soybean|maize|bajra|other),
 "event_time": ISO 8601 time of the loss with +05:30, or null,
 "deadline": ISO 8601 deadline by which the loss must be reported, or null}"""


async def run_plain(c: dict) -> dict:
    """Same farmer, same AI, no tools and no rules."""
    t0, calls = time.perf_counter(), 0
    w = W[c["lang"]]
    extra = (w["harvested"] if c.get("harvested") else w["standing"]) if c["peril"] == "rain" else ""
    story = [opening(c), f"{w[c['when']]} {w[c['peril']]}, {w[c['crop']]}. {extra} {w['nophoto']}", w["yes"]]
    convo = f"NOW: {clock.now():%A %Y-%m-%d %H:%M} IST. Farmer phone linked to PMFBY policy (crops: {', '.join(PROFILES[c['profile']])}).\n"
    claimed = False
    for msg in story:
        convo += f"Farmer: {msg}\n"
        try:
            r = await llm.generate_json("baseline_reply", BASE_SYSTEM, [convo])
            calls += 1
            reply = r.get("reply", "")
        except llm.LLMUnavailable:
            reply = ""
        convo += f"Assistant: {reply}\n"
    try:
        out = await llm.generate_json("baseline_final", BASE_FINAL, [convo])
        calls += 1
    except llm.LLMUnavailable:
        out = {}
    claimed = bool(out.get("filed")) or bool(out.get("docket"))
    exp = expected_event(c)
    dl = clock.parse(out.get("deadline")) if out.get("deadline") else None
    return {
        # No tools: nothing is ever really filed. For an area-based loss the right answer is NOT to file, so not claiming counts as correct.
        "completed": (not claimed) if c["expect"] == "area_based" else False,
        "deadline_ok": None if c["expect"] == "area_based" else bool(dl) and abs((dl - (exp + timedelta(hours=72))).total_seconds()) <= 3 * 3600,
        "fields_ok": out.get("peril") == PERIL_KEY[c["peril"]] and out.get("crop") == c["crop"],
        "recovered": None, "false_done": claimed, "secs": round(time.perf_counter() - t0, 1), "calls": calls,
        "claim": "said it was filed" + (f" (docket {out.get('docket')})" if out.get("docket") else "") if claimed else "did not claim filing",
    }


def pct(rows, key, sub=None):
    vals = [r[key] for r in rows if r[key] is not None and (sub is None or sub(r))]
    return round(100 * sum(vals) / len(vals)) if vals else 0


SCEN = {"area_based": "Rain on standing crop (area-based)", "normal": "Normal flow", "vague": "Vague first message", "fail_down": "Portal down (503)", "fail_slow": "Portal timeout", "wrong_crop": "Crop not on policy"}
LANG = {"hi": "Hindi", "mr": "Marathi", "mwr": "Marwadi"}


async def main():
    cfg = uvicorn.Config(app, port=8766, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:8766/mock/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    cases = build_cases()[: int(os.getenv("EVAL_N", "45"))]
    rows = []
    for n, c in enumerate(cases):
        ours = await run_ours(c, n)
        base = await run_plain(c)
        rows.append({"case": c, "ours": ours, "base": base})
        print(f"{c['id']} {c['lang']:3} {c['kind']:10} ours: {'OK ' if ours['completed'] else 'NO '} fields={ours['fields_ok']} deadline={ours['deadline_ok']} "
              f"({ours['secs']}s, {ours['calls']} AI calls) | plain: fields={base['fields_ok']} deadline={base['deadline_ok']} false_done={base['false_done']}", flush=True)
    O, B = [r["ours"] for r in rows], [r["base"] for r in rows]
    summary = {
        "available": True, "sample": False, "generated": clock.now().strftime("%d %b %Y, %I:%M %p"),
        "metrics": [["Task Completed %", pct(O, "completed"), pct(B, "completed")],
                    ["Correct Deadline %", pct(O, "deadline_ok"), pct(B, "deadline_ok")],
                    ["Correct Fields %", pct(O, "fields_ok"), pct(B, "fields_ok")],
                    ["Recovered from Failure %", pct(O, "recovered"), 0]],
        "calls": [round(sum(r["calls"] for r in O) / len(O), 1), round(sum(r["calls"] for r in B) / len(B), 1)],
        "time": [round(sum(r["secs"] for r in O) / len(O), 1), round(sum(r["secs"] for r in B) / len(B), 1)],
        "false_done": [sum(r["false_done"] for r in O), sum(r["false_done"] for r in B)],
        "langs": [[LANG[k], sum(1 for r in rows if r["case"]["lang"] == k)] for k in ("hi", "mr", "mwr")],
        "real_notes": 0,
        "cases": [{"id": r["case"]["id"], "lang": LANG[r["case"]["lang"]], "audio": "Text", "sc": SCEN[r["case"]["kind"]],
                   "exp": f"{r['case']['peril']} · {r['case']['crop']} · " + ("explain area-based, do not file" if r["case"]["expect"] == "area_based" else "verified filing"),
                   "ours": ("Explained area-based (not filed)" if r["case"]["expect"] == "area_based" else "Verified " + (r["ours"]["docket"] or "")) if r["ours"]["completed"] else r["ours"]["status"],
                   "base": r["base"]["claim"], "pass": r["ours"]["completed"] and r["ours"]["fields_ok"] and r["ours"]["deadline_ok"] is not False} for r in rows],
        "method": "45 scripted farmer conversations (21 Hindi, 15 Marathi, 9 Marwadi); same AI models for both; plain chatbot has no tools; automatic scoring.",
    }
    out = ROOT / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / ("summary.json" if len(cases) == 45 else "trial.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\nSUMMARY", json.dumps({k: summary[k] for k in ("metrics", "calls", "time", "false_done")}, ensure_ascii=False))
    srv.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())
