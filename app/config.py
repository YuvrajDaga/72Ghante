"""Settings read from .env. Secrets stay here and are never logged."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = Path(os.getenv("GHANTE_DB") or DATA_DIR / "ghante.db")  # tests point this at a temp file
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACK_MODELS = [m.strip() for m in _env("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
GEMINI_TTS_MODELS = [m for m in dict.fromkeys([_env("GEMINI_TTS_MODEL"), "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]) if m]
GEMINI_TTS_VOICE = _env("GEMINI_TTS_VOICE", "Kore")

TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = _env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VOICE_FROM = _env("TWILIO_VOICE_FROM")
TWILIO_SMS_FROM = _env("TWILIO_SMS_FROM")

PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
PMFBY_BASE_URL = _env("PMFBY_BASE_URL", "http://127.0.0.1:8000/mock").rstrip("/")

INTIMATION_WINDOW_HOURS = 72
HANDOFF_WINDOW_HOURS = float(_env("HANDOFF_WINDOW_HOURS", "48"))

# Optional: lets the dashboard be opened from another device through ngrok (send it as X-Admin-Key or ?key=).
ADMIN_KEY = _env("ADMIN_KEY", "")
