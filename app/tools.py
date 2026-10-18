"""Tools the agent uses. Each call is traced with its input, output, status and latency.

REAL: weather (Open-Meteo live data), photo quality checks (local), Gemini reading.
MOCK: everything under PMFBY_BASE_URL (policy lookup, intimation submit, status, officer route).
"""

import io
import time
from datetime import timedelta
from typing import Any

import httpx
from PIL import Image, ImageFilter, ImageStat

from . import clock, store
from .config import PMFBY_BASE_URL

SUBMIT_TIMEOUT_S = 8


class ToolError(Exception):
    def __init__(self, kind: str, detail: str = "", status: int | None = None, body: Any = None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.status, self.body = kind, detail, status, body


async def _call(case_id: str | None, name: str, method: str, url: str, payload: dict | None = None, timeout: float = 10) -> dict:
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.request(method, url, json=payload)
        ms = int((time.perf_counter() - t0) * 1000)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"text": resp.text[:300]}
        if resp.status_code >= 400:
            store.add_trace(case_id, "tool", name, f"http_{resp.status_code}", ms, payload, body)
            raise ToolError("http", str(resp.status_code), resp.status_code, body)
        store.add_trace(case_id, "tool", name, "ok", ms, payload, body)
        return body
    except httpx.TimeoutException:
        store.add_trace(case_id, "tool", name, "timeout", int((time.perf_counter() - t0) * 1000), payload, None)
        raise ToolError("timeout", f"no response in {timeout}s")
    except httpx.TransportError as err:
        store.add_trace(case_id, "tool", name, "network_error", int((time.perf_counter() - t0) * 1000), payload, str(err))
        raise ToolError("network", str(err))


async def portal_health(case_id: str | None) -> bool:
    """Reachability only. The agent never reads the mock's fault switches: it discovers
    failures by trying, exactly as it would against the real portal."""
    try:
        await _call(case_id, "pmfby.health_check", "GET", f"{PMFBY_BASE_URL}/health", timeout=3)
        return True
    except ToolError:
        return False


async def lookup_policy(case_id: str, mobile: str) -> dict:
    return await _call(case_id, "pmfby.policy_lookup", "POST", f"{PMFBY_BASE_URL}/api/v1/policy/lookup", {"mobile": mobile})


async def submit_intimation(case_id: str, payload: dict) -> dict:
    return await _call(case_id, "pmfby.submit_intimation", "POST", f"{PMFBY_BASE_URL}/api/v1/intimation", payload, timeout=SUBMIT_TIMEOUT_S)


async def intimation_status(case_id: str, docket: str) -> dict:
    return await _call(case_id, "pmfby.intimation_status", "GET", f"{PMFBY_BASE_URL}/api/v1/intimation/{docket}", timeout=8)


async def officer_report(case_id: str, payload: dict) -> dict:
    return await _call(case_id, "officer.create_report", "POST", f"{PMFBY_BASE_URL}/api/v1/officer/report", payload)


async def officer_report_status(case_id: str, report_id: str) -> dict:
    return await _call(case_id, "officer.report_status", "GET", f"{PMFBY_BASE_URL}/api/v1/officer/report/{report_id}")


# ---- Weather corroboration (REAL, live Open-Meteo data). A confidence signal only, never a gate. ----

PLACES = {
    "lasalgaon": (20.15, 74.23), "niphad": (20.08, 74.11), "dindori": (20.20, 73.83), "mohadi": (20.17, 73.86),
    "nashik": (19.99, 73.79), "osian": (26.72, 72.92), "tinwari": (26.53, 72.96), "jodhpur": (26.24, 73.02),
}
HAIL_CODES = {96, 99}
RAIN_CODES = {61, 63, 65, 80, 81, 82, 95, 96, 99}


async def weather_check(case_id: str, place: str, event_iso: str) -> dict:
    lat, lon = PLACES.get((place or "").lower(), PLACES["nashik"])
    # Live data is in real time; remove any demo time-travel offset first.
    event = clock.parse(event_iso) - timedelta(hours=clock.offset_hours())
    t0 = time.perf_counter()
    params = {"latitude": lat, "longitude": lon, "hourly": "precipitation,weather_code", "past_days": 7, "forecast_days": 1, "timezone": "Asia/Kolkata"}
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            resp = await http.get("https://api.open-meteo.com/v1/forecast", params=params)
        data = resp.json()["hourly"]
    except Exception as err:  # noqa: BLE001 - weather is optional
        store.add_trace(case_id, "tool", "weather.open_meteo", "error", int((time.perf_counter() - t0) * 1000), params, str(err)[:200])
        return {"available": False}
    lo, hi = event - timedelta(hours=12), event + timedelta(hours=12)
    rain_mm, codes = 0.0, set()
    for t, mm, code in zip(data["time"], data["precipitation"], data["weather_code"]):
        ts = clock.parse(t)
        if lo <= ts <= hi:
            rain_mm += mm or 0
            codes.add(code)
    result = {
        "available": True, "place": place or "nashik", "rain_mm_24h": round(rain_mm, 1),
        "hail_signal": bool(codes & HAIL_CODES), "rain_signal": rain_mm >= 2 or bool(codes & RAIN_CODES),
    }
    store.add_trace(case_id, "tool", "weather.open_meteo", "ok", int((time.perf_counter() - t0) * 1000), params, result)
    return result


# ---- Photo quality (REAL, local) ----

def photo_quality(image: bytes) -> dict:
    """Brightness and sharpness from pixels. WhatsApp strips EXIF (time/GPS) from photos,
    so the capture time we can trust is the time the photo reached us."""
    img = Image.open(io.BytesIO(image)).convert("L")
    w, h = img.size
    small = img.resize((min(w, 640), int(h * min(w, 640) / w)))
    brightness = ImageStat.Stat(small).mean[0]
    edges = small.filter(ImageFilter.FIND_EDGES)
    sharpness = ImageStat.Stat(edges).var[0]
    problems = []
    if brightness < 45:
        problems.append("too_dark")
    if brightness > 235:
        problems.append("overexposed")
    if sharpness < 60:
        problems.append("blurry")
    if min(w, h) < 320:
        problems.append("too_small")
    return {"width": w, "height": h, "brightness": round(brightness, 1), "sharpness": round(sharpness, 1), "problems": problems}
