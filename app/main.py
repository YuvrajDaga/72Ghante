"""Web server: WhatsApp / voice-call / SMS webhooks, dashboard APIs, the PMFBY mock, and the background tick.

Run:  .venv\\Scripts\\python -m uvicorn app.main:app --port 8000
"""

import asyncio
import contextlib
import hashlib
import hmac
import os
import json
from xml.sax.saxutils import escape

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from mock_pmfby import server as mock_server

from . import agent, channels, clock, demo_roll, stage, store, tools
from . import stats as stats_mod
from .api_cases import router as cases_router
from .config import ADMIN_KEY, ADMIN_PASSWORD, DATA_DIR, MEDIA_DIR, PUBLIC_BASE_URL, ROOT, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VOICE_FROM

TICK_SECONDS = 5


async def _ticker():
    n = 0
    while True:
        try:
            if n % 120 == 0:  # every ~10 minutes: keep demo dates current
                demo_roll.roll()
            n += 1
            await agent.tick()
        except Exception as err:  # noqa: BLE001 - the loop must survive any single failure
            store.add_trace(None, "system", "tick_error", "error", None, None, f"{type(err).__name__}: {err}")
        await asyncio.sleep(TICK_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("SEED_DEMO_ON_START") == "1" and not store._db.execute("SELECT 1 FROM cases WHERE id LIKE 'CG-2026-%' LIMIT 1").fetchone():
        from eval import seed_demo  # first start on a new server: create the 842 demo cases
        seed_demo.main()
    task = asyncio.create_task(_ticker())
    yield
    task.cancel()


app = FastAPI(title="72 Ghante", lifespan=lifespan)
app.include_router(cases_router)
app.mount("/mock", mock_server.mock)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/static", StaticFiles(directory=ROOT / "dashboard"), name="static")


# Phone webhooks Twilio must reach from the internet; everything else is the admin control room.
PUBLIC_PATHS = ("/whatsapp", "/sms", "/voice/", "/media/")


@app.middleware("http")
async def admin_only_from_this_laptop(request, call_next):
    """The control room (dashboard + every /api call, including /api/reset) only answers on this laptop.
    When the server is opened to the internet (ngrok for WhatsApp), outside visitors get only the webhooks,
    unless they send the ADMIN_KEY from .env (header X-Admin-Key or ?key=)."""
    host = request.client.host if request.client else ""
    outside = bool(request.headers.get("x-forwarded-for")) or host not in ("127.0.0.1", "::1", "localhost", "testclient")
    path = request.url.path
    if outside and not path.startswith(PUBLIC_PATHS) and path not in ("/login", "/static/img/logo.png"):
        key = request.headers.get("x-admin-key") or request.query_params.get("key")
        signed_in = ADMIN_PASSWORD and hmac.compare_digest(request.cookies.get("gh_auth", ""), _auth_token())
        if not (signed_in or (ADMIN_KEY and key == ADMIN_KEY)):
            if ADMIN_PASSWORD and request.method == "GET" and (path == "/" or path.endswith(".html")):
                return RedirectResponse("/login", 303)
            return JSONResponse({"error": "Sign in first" if ADMIN_PASSWORD else "Admin area: open it on the demo laptop"}, 401 if ADMIN_PASSWORD else 403)
    return await call_next(request)


# ---------------------------------------------------------------- password page (public deployment)

def _auth_token() -> str:
    return hmac.new(ADMIN_PASSWORD.encode(), b"72ghante-admin", hashlib.sha256).hexdigest()


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>72 Ghante – Sign in</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;color:#fff;
background:radial-gradient(circle at 30% 20%,#0f3b2c,#04120d 70%)}
form{width:320px;padding:30px 28px;border-radius:16px;background:rgba(8,28,30,.85);border:1px solid rgba(60,220,160,.5);box-shadow:0 0 30px rgba(40,230,160,.2);text-align:center}
img{width:64px}h1{margin:8px 0 2px;font-size:24px}p{margin:0 0 20px;color:#bcd0ca;font-size:13px}
input{width:100%;box-sizing:border-box;height:42px;border-radius:9px;border:1px solid rgba(110,170,210,.4);background:#0a1e26;color:#fff;padding:0 12px;font-size:15px}
button{margin-top:14px;width:100%;height:42px;border:0;border-radius:9px;font-weight:700;font-size:15px;color:#05281a;background:linear-gradient(180deg,#4ff6bd,#2fdc9f);cursor:pointer}
.e{color:#ff6b78;font-size:13px;min-height:18px;margin-top:10px}</style></head><body>
<form method="post" action="/login"><img src="/static/img/logo.png" alt=""><h1>72 Ghante</h1><p>Control room · admin sign in</p>
<input type="password" name="password" placeholder="Password" autofocus required><button>Sign in</button><div class="e">{err}</div></form></body></html>"""


@app.get("/login")
async def login_page():
    return Response(LOGIN_HTML.replace("{err}", ""), media_type="text/html")


@app.post("/login")
async def login(password: str = Form("")):
    if not ADMIN_PASSWORD or not hmac.compare_digest(password, ADMIN_PASSWORD):
        await asyncio.sleep(1.5)  # slows down password guessing
        return Response(LOGIN_HTML.replace("{err}", "Wrong password"), media_type="text/html", status_code=401)
    resp = RedirectResponse("/static/new.html#dashboard", 303)
    resp.set_cookie("gh_auth", _auth_token(), max_age=7 * 86400, httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("gh_auth")
    return resp


@app.middleware("http")
async def no_stale_dashboard(request, call_next):
    """Browsers must always load the newest dashboard page (old cached copies had dead buttons)."""
    resp = await call_next(request)
    if request.url.path.endswith(".html") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _phone(raw: str) -> str:
    return raw.replace("whatsapp:", "").strip()


async def _download(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "application/octet-stream").split(";")[0]


async def _process_twilio_message(form: dict, channel: str) -> None:
    phone = _phone(form.get("From", ""))
    media = []
    for i in range(int(form.get("NumMedia", "0") or 0)):
        try:
            media.append(await _download(form[f"MediaUrl{i}"]))
        except Exception as err:  # noqa: BLE001
            store.add_trace(None, "channel", f"{channel}.media_download", "error", None, None, str(err)[:200])
    await agent.handle_inbound(phone, channel, form.get("Body") or None, media)


EMPTY_TWIML = Response("<Response></Response>", media_type="application/xml")


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request, background: BackgroundTasks):
    # Answer Twilio at once; the agent replies asynchronously through the REST API.
    form = dict(await request.form())
    background.add_task(_process_twilio_message, form, "whatsapp")
    return EMPTY_TWIML


@app.post("/sms")
async def sms_webhook(request: Request, background: BackgroundTasks):
    form = dict(await request.form())
    background.add_task(_process_twilio_message, form, "sms")
    return EMPTY_TWIML


# ---------------------------------------------------------------- phone call (for farmers without a smartphone)

def _twiml(*parts: str) -> Response:
    return Response("<Response>" + "".join(parts) + "</Response>", media_type="application/xml")


def _record() -> str:
    return f'<Record action="{PUBLIC_BASE_URL}/voice/recorded" maxLength="30" timeout="3" playBeep="true" trim="trim-silence"/>'


def _call_phone(form: dict) -> str:
    return form.get("To") if form.get("Direction", "").startswith("outbound") else form.get("From", "")


@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    form = dict(await request.form())
    phone = _call_phone(form)
    channels.voice_queue[phone].clear()
    greeting = "Namaskar. 72 Ghante mein aapka swagat hai. Beep ke baad batayiye, aapki fasal ko kya nuksaan hua aur kab hua."
    return _twiml(f'<Say language="hi-IN">{escape(greeting)}</Say>', _record())


@app.post("/voice/recorded")
async def voice_recorded(request: Request, background: BackgroundTasks):
    form = dict(await request.form())
    phone = _call_phone(form)

    async def run():
        audio = await _download(form["RecordingUrl"] + ".mp3")
        await agent.handle_inbound(phone, "voice", None, [(audio[0], "audio/mpeg")])

    background.add_task(run)
    return _twiml('<Say language="hi-IN">Ek minute.</Say>', f'<Redirect method="POST">{PUBLIC_BASE_URL}/voice/poll?n=0</Redirect>')


@app.post("/voice/poll")
async def voice_poll(request: Request, n: int = 0):
    form = dict(await request.form())
    phone = _call_phone(form)
    queue = channels.voice_queue[phone]
    if queue:
        parts = []
        while queue:
            item = queue.pop(0)
            if item.get("audio"):
                parts.append(f"<Play>{PUBLIC_BASE_URL}{item['audio']}</Play>")
            else:
                parts.append(f'<Say language="hi-IN">{escape(item["text"])}</Say>')
        return _twiml(*parts, _record())
    if n < 20:
        return _twiml('<Pause length="2"/>', f'<Redirect method="POST">{PUBLIC_BASE_URL}/voice/poll?n={n + 1}</Redirect>')
    return _twiml('<Say language="hi-IN">Maaf kijiye, thodi der lag rahi hai. Kripya dobara boliye.</Say>', _record())


@app.post("/api/call")
async def start_call(body: dict):
    """Agent calls the farmer (missed-call callback, or for the demo)."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_VOICE_FROM and PUBLIC_BASE_URL):
        return JSONResponse({"error": "Set TWILIO_VOICE_FROM and PUBLIC_BASE_URL in .env"}, 400)
    async with httpx.AsyncClient(timeout=15, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)) as http:
        resp = await http.post(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json",
                               data={"To": body["phone"], "From": TWILIO_VOICE_FROM, "Url": f"{PUBLIC_BASE_URL}/voice/incoming"})
    store.add_trace(None, "channel", "voice.outbound_call", "ok" if resp.status_code < 300 else "error", None, {"to": store.mask_phone(body["phone"])}, resp.status_code)
    return JSONResponse(resp.json(), resp.status_code)


# ---------------------------------------------------------------- simulator (test without a phone)

@app.post("/api/sim/send")
async def sim_send(phone: str = Form(...), text: str = Form(""), voice: bool = Form(False), file: UploadFile | None = File(None)):
    media = []
    if file is not None:
        data = await file.read()
        mime = file.content_type or "application/octet-stream"
        if mime.startswith("audio") and "ogg" not in mime:
            # Browser recordings are WebM; Gemini and WhatsApp both want OGG/Opus voice notes.
            data, mime = await asyncio.to_thread(_to_ogg, data), "audio/ogg"
        media.append((data, mime))
    case = store.open_case_for(phone)
    if case is not None and case.get("sim_voice") != voice:
        case["sim_voice"] = voice
        store.save_case(case)
    case = await agent.handle_inbound(phone, "sim", text or None, media)
    return {"case_id": case["id"]}


def _to_ogg(data: bytes) -> bytes:
    import subprocess
    import imageio_ffmpeg
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                          input=data, capture_output=True, check=True).stdout


@app.get("/api/sim/mode")
async def sim_mode():
    return {"script": stage.is_on()}


@app.post("/api/sim/mode")
async def sim_mode_set(body: dict):
    stage.set_on(bool(body.get("script")))
    if stage.is_on():  # start the stage demo from a clean chat
        await sim_reset({"phone": body.get("phone") or "+919000000001"})
    return {"script": stage.is_on()}


@app.post("/api/sim/clear")
async def sim_clear(body: dict):
    return await sim_reset({"phone": body.get("phone") or "+919000000001"})


@app.post("/api/sim/script")
async def sim_script(phone: str = Form(...), text: str = Form(""), as_text: bool = Form(False)):
    """Stage script mode (backup): next fixed farmer step + fixed agent reply, no AI."""
    return await stage.step(phone, text or None, as_text, reset=lambda ph: sim_reset({"phone": ph}))


@app.post("/api/sim/reset")
async def sim_reset(body: dict):
    """Start a fresh live-demo chat for this phone: closes its open case and clears that farmer's earlier
    filings in the MOCK portal (so a repeat demo is not treated as a duplicate)."""
    phone = body.get("phone", "")
    case = store.open_case_for(phone)
    if case:
        app_no = case["fields"].get("application_no")
        case["stage"] = "closed"
        agent.log(case, "done", "Live demo chat restarted by admin; this case was closed")
        store.save_case(case)
        if app_no:
            mock_server.state["intimations"] = {k: v for k, v in mock_server.state["intimations"].items() if v.get("application_no") != app_no}
            mock_server._save()
    return {"ok": True}


@app.get("/api/sim/open")
async def sim_open(phone: str):
    """The live demo chat shows only the farmer's current open case."""
    case = store.open_case_for(phone)
    return {"case_id": case["id"] if case else None}


@app.get("/api/sim/messages")
async def sim_messages(phone: str, after: int = 0):
    return store.messages_for(phone, after)


# ---------------------------------------------------------------- dashboard APIs

# ---------------------------------------------------------------- map tiles, cached on disk (map works offline at the venue)

TILE_SRC = {
    "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "night": "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png",
    "dark": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
}
TILE_DIR = DATA_DIR / "tiles"


async def fetch_tile(src: str, z: int, x: int, y: int) -> bytes | None:
    path = TILE_DIR / src / str(z) / str(x) / f"{y}.img"
    if path.exists():
        return path.read_bytes()
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "72Ghante-dashboard"}) as http:
            r = await http.get(TILE_SRC[src].format(z=z, x=x, y=y))
        if r.status_code != 200 or not r.content:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return r.content
    except httpx.HTTPError:
        return None


@app.get("/tiles/{src}/{z}/{x}/{y}")
async def tile(src: str, z: int, x: int, y: int):
    if src not in TILE_SRC:
        return Response(status_code=404)
    data = await fetch_tile(src, z, x, y)
    if data is None:
        return Response(status_code=404)
    kind = "image/png" if data[1:4] == b"PNG" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/stats")
async def stats():
    return stats_mod.compute()


@app.get("/api/weather")
async def weather_now(place: str = "nashik"):
    """Current live weather for the top bar (Open-Meteo, no key)."""
    lat, lon = tools.PLACES.get(place.lower(), tools.PLACES["nashik"])
    try:
        async with httpx.AsyncClient(timeout=6) as http:
            r = await http.get("https://api.open-meteo.com/v1/forecast", params={"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code", "timezone": "Asia/Kolkata"})
        cur = r.json()["current"]
        return {"available": True, "place": place.title(), "temp_c": round(cur["temperature_2m"]), "code": cur["weather_code"]}
    except Exception:  # noqa: BLE001 - weather chip is decorative
        return {"available": False}


@app.get("/api/trace")
async def trace(after: int = 0, limit: int = 200):
    return store.trace_for(None, after, limit)


@app.get("/api/live_chat")
async def live_chat():
    """The most recent live (non-demo) conversation, mirrored on the projector during the demo."""
    live = [c for c in store.list_cases() if not c.get("demo")]
    if not live:
        return {"case_id": None, "messages": []}
    return {"case_id": live[0]["id"], "phone": store.mask_phone(live[0]["phone"]), "channel": live[0]["channel"],
            "messages": store.messages_for_case(live[0]["id"])}


@app.get("/api/eval")
async def eval_results():
    path = ROOT / "eval" / "results" / "summary.json"
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"available": False})


@app.get("/api/clock")
async def get_clock():
    return {"now": clock.now().isoformat(timespec="seconds"), "offset_hours": clock.offset_hours()}


@app.post("/api/clock/advance")
async def advance_clock(body: dict):
    clock.advance(float(body.get("hours", 0)))
    store.add_trace(None, "system", "time_travel", "ok", None, body, {"offset_hours": clock.offset_hours()})
    return await get_clock()


@app.post("/api/clock/reset")
async def reset_clock():
    clock.reset()
    return await get_clock()


@app.post("/api/faults")
async def faults(body: dict):
    result = await mock_server.admin_faults(body)
    store.add_trace(None, "system", "fault_injection", "warn" if "down" in result.values() or "flaky" in result.values() else "ok", None, body, result)
    return result


@app.get("/api/mock")
async def mock_state():
    return await mock_server.admin_state()


@app.post("/api/link")
async def link(body: dict):
    return await mock_server.admin_link(body)


@app.post("/api/officer_forward")
async def officer_forward(body: dict):
    return await mock_server.admin_officer_forward(body)


@app.post("/api/reset")
async def reset_all():
    store.reset_all()
    await mock_server.admin_reset()
    clock.reset()
    return {"reset": True}


@app.get("/")
async def dashboard():
    # No caching, so a refresh always shows the latest dashboard.
    return RedirectResponse("/static/new.html#dashboard", 307)


# ---- Tools & Integrations: live health checks (real measurements, kept in memory) ----
import time as _time
from collections import deque as _deque
from . import config as _cfg
_HEALTH: dict[str, _deque] = {}


async def _probe(name: str, fn) -> None:
    t0 = _time.perf_counter()
    try:
        ok = await fn()
    except Exception:  # noqa: BLE001 - a failed probe is a result, not an error
        ok = False
    _HEALTH.setdefault(name, _deque(maxlen=60)).append({"ts": clock.now().isoformat(), "ok": bool(ok), "ms": round((_time.perf_counter() - t0) * 1000)})


@app.get("/api/tools/health")
async def tools_health():
    async def weather():
        async with httpx.AsyncClient(timeout=6) as http:
            r = await http.get("https://api.open-meteo.com/v1/forecast", params={"latitude": 20.0, "longitude": 73.8, "current": "temperature_2m"})
        return r.status_code == 200

    async def pmfby():
        async with httpx.AsyncClient(timeout=4) as http:
            r = await http.get(_cfg.PMFBY_BASE_URL.rstrip("/") + "/health")
        return r.status_code == 200

    async def gemini():
        if not _cfg.GEMINI_API_KEY:
            return False
        async with httpx.AsyncClient(timeout=6) as http:
            r = await http.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": _cfg.GEMINI_API_KEY, "pageSize": 1})
        return r.status_code == 200

    async def twilio():
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
            return False
        async with httpx.AsyncClient(timeout=6, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)) as http:
            r = await http.get(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}.json")
        return r.status_code == 200

    await asyncio.gather(_probe("weather", weather), _probe("pmfby", pmfby), _probe("gemini", gemini), _probe("twilio", twilio))
    calls = store.trace_for(None, 0, 5000)
    out = {}
    for k, h in _HEALTH.items():
        oks = [x for x in h if x["ok"]]
        out[k] = {"checks": len(h), "uptime": round(100 * len(oks) / len(h), 1) if h else None,
                  "avg_ms": round(sum(x["ms"] for x in oks) / len(oks)) if oks else None, "last": h[-1], "history": list(h)}
    return {"probes": out, "configured": {"gemini": bool(_cfg.GEMINI_API_KEY), "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
            "pmfby_mock": "/mock" in _cfg.PMFBY_BASE_URL}, "agent_calls": len(calls), "agent_errors": sum(1 for c in calls if c.get("status") != "ok")}


# ---- Team & Access: real members and role permissions, saved to data/team.json ----
_TEAM_FILE = _cfg.DATA_DIR / "team.json"
_PERMS = ["View cases", "Edit cases", "Escalate to higher authority", "Run simulations", "See farmer phone numbers",
          "Manage team members", "Access reports & analytics", "Modify system settings"]
_ROLES = ["Admin", "District Officer", "Helpline Staff", "Surveyor"]


def _team_load() -> dict:
    if _TEAM_FILE.exists():
        return json.loads(_TEAM_FILE.read_text(encoding="utf-8"))
    default = {r: [True] * len(_PERMS) if r == "Admin" else [i in {"District Officer": (0, 1, 2, 3, 4, 6), "Helpline Staff": (0, 2, 4), "Surveyor": (0,)}[r] for i in range(len(_PERMS))] for r in _ROLES}
    return {"members": [{"name": "Yuvraj Daga", "email": "", "role": "Admin", "district": "All Districts", "status": "Active", "added": clock.now().isoformat(), "owner": True}],
            "perms": default, "log": [{"ts": clock.now().isoformat(), "who": "System", "what": "Control room created", "detail": "Yuvraj Daga set as Admin"}]}


def _team_save(t: dict) -> None:
    _TEAM_FILE.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")


@app.get("/api/team")
async def team_get():
    return {**_team_load(), "permissions": _PERMS, "roles": _ROLES}


@app.post("/api/team/member")
async def team_add(body: dict):
    t = _team_load()
    name = (body.get("name") or "").strip()
    if not name or body.get("role") not in _ROLES:
        return JSONResponse({"detail": "Name and a valid role are required"}, 422)
    t["members"].append({"name": name, "email": (body.get("email") or "").strip(), "role": body["role"], "district": (body.get("district") or "All Districts").strip(),
                         "status": "Invited", "added": clock.now().isoformat()})
    t["log"].insert(0, {"ts": clock.now().isoformat(), "who": "Yuvraj Daga", "what": f"Invited {name}", "detail": f"Role: {body['role']}"})
    _team_save(t)
    return {"ok": True}


@app.post("/api/team/perm")
async def team_perm(body: dict):
    t = _team_load()
    role, i, on = body.get("role"), int(body.get("i", -1)), bool(body.get("on"))
    if role not in _ROLES or not 0 <= i < len(_PERMS) or role == "Admin":
        return JSONResponse({"detail": "Admin always has every permission"}, 422)
    t["perms"][role][i] = on
    t["log"].insert(0, {"ts": clock.now().isoformat(), "who": "Yuvraj Daga", "what": f"{'Allowed' if on else 'Removed'} '{_PERMS[i]}'", "detail": f"For role {role}"})
    _team_save(t)
    return {"ok": True}


@app.get("/api/weather/alerts")
async def weather_alerts(region: str = "mh"):
    """Real 3-day forecast alerts (Open-Meteo) for the region's talukas: hail/thunder, heavy rain, strong wind."""
    spots = {"mh": {"Nashik": (20.0, 73.79), "Malegaon": (20.55, 74.53), "Niphad": (20.08, 74.11), "Sinnar": (19.85, 74.0), "Yeola": (20.04, 74.49)},
             "rj": {"Jodhpur": (26.24, 73.02), "Osian": (26.72, 72.92), "Phalodi": (27.13, 72.36), "Bilara": (26.18, 73.7)}}.get(region, {})
    out = []
    async with httpx.AsyncClient(timeout=8) as http:
        for name, (lat, lon) in spots.items():
            try:
                r = await http.get("https://api.open-meteo.com/v1/forecast", params={"latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata", "forecast_days": 3,
                                   "daily": "weather_code,precipitation_sum,wind_gusts_10m_max"})
                d = r.json()["daily"]
            except Exception:  # noqa: BLE001
                continue
            for i, day in enumerate(d["time"]):
                code, rain, gust = d["weather_code"][i], d["precipitation_sum"][i] or 0, d["wind_gusts_10m_max"][i] or 0
                if code in (95, 96, 99):
                    out.append({"place": name, "day": day, "kind": "Thunderstorm / hail risk", "level": "high", "detail": f"Weather code {code}, gusts up to {gust:.0f} km/h, rain {rain:.0f} mm"})
                elif rain >= 20:
                    out.append({"place": name, "day": day, "kind": "Heavy rainfall", "level": "medium", "detail": f"{rain:.0f} mm expected, gusts {gust:.0f} km/h"})
                elif gust >= 45:
                    out.append({"place": name, "day": day, "kind": "Strong wind", "level": "medium", "detail": f"Gusts up to {gust:.0f} km/h"})
    return {"region": region, "alerts": out, "checked": clock.now().isoformat(), "source": "Open-Meteo 3-day forecast"}


# ---- Settings page preferences, saved to data/prefs.json ----
_PREFS_FILE = _cfg.DATA_DIR / "prefs.json"
_PREFS_DEFAULT = {"languages": {"mr": True, "hi": True, "mwr": True}, "at_risk_hours": 12, "reminder_hours": 6,
                  "channels": {"whatsapp": True, "voice": True, "sms": True}, "retention_days": 90, "mask_phones": True, "allow_report_download": True,
                  "consent": {"whatsapp": "नमस्कार शेतकरी मित्रा,\nतुमच्या पीक नुकसान भरपाईचा अर्ज 72 तासांच्या आत भरण्यासाठी आम्ही तुमची माहिती वापरणार आहोत.\nही माहिती फक्त सरकारी पोर्टलवर अर्ज भरण्यासाठी वापरली जाईल. तुमची माहिती सुरक्षित आहे आणि इतर कोणत्याही उद्देशासाठी वापरली जाणार नाही.\nतुम्ही पुढे सुरु ठेवण्यासाठी \"होय\" असे उत्तर द्या.",
                              "voice": "Namaste. Aapki fasal nuksan ki soochna 72 ghante mein darj karne ke liye hum aapki jaankari ka upyog karenge. Aage badhne ke liye 'haan' bolein.",
                              "sms": "72 Ghante: We will use your details only to file your crop-loss intimation within 72 hours. Reply YES to continue."}}


@app.get("/api/prefs")
async def prefs_get():
    saved = json.loads(_PREFS_FILE.read_text(encoding="utf-8")) if _PREFS_FILE.exists() else {}
    return {**_PREFS_DEFAULT, **saved, "intimation_window_hours": 72, "handoff_window_hours": _cfg.HANDOFF_WINDOW_HOURS,
            "keys": {"gemini": bool(_cfg.GEMINI_API_KEY), "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)}}


@app.post("/api/prefs")
async def prefs_set(body: dict):
    saved = json.loads(_PREFS_FILE.read_text(encoding="utf-8")) if _PREFS_FILE.exists() else {}
    for k in _PREFS_DEFAULT:
        if k in body:
            saved[k] = body[k]
    _PREFS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}
