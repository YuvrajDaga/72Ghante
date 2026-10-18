"""Sends the agent's replies on the farmer's channel: WhatsApp, phone call, SMS, or the dashboard simulator.

One agent, many channels. A voice reply is a voice note on WhatsApp and spoken audio on a
call; every reply is also stored, so the dashboard shows exactly what the farmer received.
"""

import uuid
from collections import defaultdict

import httpx

from . import llm, store
from .config import MEDIA_DIR, PUBLIC_BASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_SMS_FROM, TWILIO_WHATSAPP_FROM

# Replies waiting to be spoken on a live call, per phone number.
voice_queue: dict[str, list[dict]] = defaultdict(list)


def save_media(data: bytes, ext: str) -> str:
    name = f"{uuid.uuid4().hex}.{ext}"
    (MEDIA_DIR / name).write_bytes(data)
    return f"/media/{name}"


def public(path: str) -> str | None:
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else None


async def twilio_send(to: str, body: str, media_url: str | None, whatsapp: bool) -> dict:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return {"sent": False, "reason": "twilio not configured"}
    data = {"From": TWILIO_WHATSAPP_FROM if whatsapp else TWILIO_SMS_FROM, "To": (f"whatsapp:{to}" if whatsapp else to), "Body": body}
    if media_url:
        data["MediaUrl"] = media_url
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient(timeout=15, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)) as http:
        resp = await http.post(url, data=data)
    ok = resp.status_code < 300
    return {"sent": ok, "status": resp.status_code, "error": None if ok else resp.json().get("message")}


async def send(case: dict, text: str, voice: bool = True, also_sms: bool = False) -> None:
    """Sends one reply. Voice is best-effort: if speech fails, the text still goes."""
    channel, phone = case["channel"], case["phone"]
    media_path = None
    if voice and channel in ("whatsapp", "voice", "sim"):
        ogg = await llm.tts(text, case["id"]) if channel != "sim" or case.get("sim_voice") else None
        if ogg:
            media_path = save_media(llm.to_mp3_from_ogg(ogg), "mp3") if channel == "voice" else save_media(ogg, "ogg")
    store.add_message(case["id"], phone, channel, "out", "voice" if media_path else "text", text, media_path)

    if channel == "whatsapp":
        # Text first (so numbers can be read), then the voice note.
        result = await twilio_send(phone, text, None, whatsapp=True)
        if media_path and public(media_path):
            await twilio_send(phone, "", public(media_path), whatsapp=True)
        store.add_trace(case["id"], "channel", "whatsapp.send", "ok" if result.get("sent") else "error", None, {"to": store.mask_phone(phone)}, result)
    elif channel == "voice":
        voice_queue[phone].append({"text": text, "audio": media_path})
    if also_sms or channel == "sms":
        # Simulator farmers have fake numbers: log the SMS copy, never send it.
        result = {"sent": False, "reason": "simulator"} if channel == "sim" else await twilio_send(phone, text, None, whatsapp=False)
        store.add_message(case["id"], phone, "sms", "out", "text", text)
        # India requires DLT sender registration for SMS; a trial number may be blocked. We record the outcome honestly.
        store.add_trace(case["id"], "channel", "sms.send", "ok" if result.get("sent") else "error", None, {"to": store.mask_phone(phone)}, result)
