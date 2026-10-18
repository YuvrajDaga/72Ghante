"""Gemini calls: JSON understanding, speech-to-text, image/document reading, text-to-speech.

Every call is traced (model, latency, status). On 429/503/timeout the call moves to the
next fallback model, so one busy model does not stop a farmer's case.
"""

import asyncio
import json
import subprocess
import time
from typing import Any

import imageio_ffmpeg
from google import genai
from google.genai import errors, types

from . import store
from .config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL, GEMINI_TTS_MODELS, GEMINI_TTS_VOICE

ATTEMPT_TIMEOUT_S = 25

_client: genai.Client | None = None


class LLMUnavailable(Exception):
    pass


def client() -> genai.Client:
    global _client
    if not GEMINI_API_KEY:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    if _client is None:
        # Hard HTTP timeout and no hidden SDK retries: our own loop decides when to move to the next model.
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(
            timeout=ATTEMPT_TIMEOUT_S * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
    return _client


def _models() -> list[str]:
    return list(dict.fromkeys([GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]))


def _retryable(err: Exception) -> bool:
    if isinstance(err, asyncio.TimeoutError):
        return True
    if isinstance(err, errors.APIError):
        return err.code in (404, 429, 500, 503, 504)
    return False


async def generate_json(name: str, system: str, parts: list[Any], case_id: str | None = None, schema: dict | None = None) -> dict:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.1,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
    )
    last_err: Exception | None = None
    for model in _models():
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(client().aio.models.generate_content(model=model, contents=parts, config=config), ATTEMPT_TIMEOUT_S)
            data = json.loads(resp.text or "{}")
            store.add_trace(case_id, "llm", name, "ok", int((time.perf_counter() - t0) * 1000), {"model": model}, data)
            return data
        except Exception as err:  # noqa: BLE001 - classified below
            last_err = err
            store.add_trace(case_id, "llm", name, "error", int((time.perf_counter() - t0) * 1000), {"model": model}, f"{type(err).__name__}: {str(err)[:300]}")
            if isinstance(err, json.JSONDecodeError) or _retryable(err):
                continue
            break
    raise LLMUnavailable(str(last_err))


TRANSCRIBE_SYSTEM = """You transcribe WhatsApp voice notes and phone-call audio from Indian farmers.
Write exactly what was said, in the original language and its native script (Marathi and Hindi in
Devanagari, Marwadi/Rajasthani in Devanagari, Gujarati in Gujarati script, etc.). Keep code-mixed
English words as spoken. Do not translate or summarise. If parts are inaudible, write [inaudible].
Return JSON: {"transcript": str, "language": ISO-like code (mr, hi, mwr for Marwadi, gu, ta, te, kn, bn, pa, or, ml, en),
"language_name": str, "audio_quality": "good"|"noisy"|"poor", "confidence": 0..1}"""


async def transcribe(audio: bytes, mime: str, case_id: str | None) -> dict:
    parts = [types.Part.from_bytes(data=audio, mime_type=mime), "Transcribe this audio."]
    return await generate_json("speech_to_text", TRANSCRIBE_SYSTEM, parts, case_id)


IMAGE_SYSTEM = """You look at a photo a farmer sent while reporting crop loss under PMFBY (Indian crop insurance).
Decide what it is and extract only what is visible. Never guess numbers that are not readable.
Return JSON:
{"kind": "crop_damage_photo" | "insurance_document" | "bank_passbook" | "other",
 "crop_visible": str|null, "damage_visible": bool, "damage_description": str|null,
 "document_fields": {"application_no": str|null, "farmer_name": str|null, "survey_no": str|null,
                     "insurer": str|null, "crop": str|null, "area_acres": number|null, "bank": str|null, "season": str|null},
 "readable": bool, "problems": [str], "summary": str}"""


async def analyse_image(image: bytes, mime: str, case_id: str | None) -> dict:
    parts = [types.Part.from_bytes(data=image, mime_type=mime), "Analyse this image."]
    return await generate_json("read_image", IMAGE_SYSTEM, parts, case_id)


async def tts(text: str, case_id: str | None) -> bytes | None:
    """Speaks the text (any Indian language, the model infers it from the script) and
    returns OGG/Opus audio, the format WhatsApp plays as a voice note. None if TTS fails:
    the farmer still gets the text, so a voice failure never blocks the case."""
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_TTS_VOICE))),
    )
    prompt = f"Speak warmly, slowly and clearly, like a helpful village officer. Say numbers digit by digit: {text}"
    for model in GEMINI_TTS_MODELS:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(client().aio.models.generate_content(model=model, contents=prompt, config=config), ATTEMPT_TIMEOUT_S)
            pcm = resp.candidates[0].content.parts[0].inline_data.data
            ogg = await asyncio.to_thread(_pcm_to(pcm, "ogg"))
            store.add_trace(case_id, "llm", "text_to_speech", "ok", int((time.perf_counter() - t0) * 1000), {"model": model, "chars": len(text)}, {"bytes": len(ogg)})
            return ogg
        except Exception as err:  # noqa: BLE001
            store.add_trace(case_id, "llm", "text_to_speech", "error", int((time.perf_counter() - t0) * 1000), {"model": model}, f"{type(err).__name__}: {str(err)[:200]}")
    return None


def _pcm_to(pcm: bytes, fmt: str):
    def run() -> bytes:
        codec = ["-c:a", "libopus", "-b:a", "32k", "-f", "ogg"] if fmt == "ogg" else ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"]
        proc = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0", *codec, "pipe:1"],
            input=pcm, capture_output=True, check=True,
        )
        return proc.stdout

    return run


def to_mp3_from_ogg(ogg: bytes) -> bytes:
    """Phone calls (Twilio <Play>) need MP3/WAV, not OGG."""
    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        input=ogg, capture_output=True, check=True,
    )
    return proc.stdout
