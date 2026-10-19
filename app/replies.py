"""Writes the agent's replies in the farmer's language.

The agent decides WHAT to say (a reply key + facts). Gemini only phrases it, using those
facts and nothing else. Exact numbers (docket, hours left) are added again as a fixed
text line by code, so a phrasing mistake can never change a number.
"""

import json

from . import llm, store
from .understand import LANGUAGES

SYSTEM = """You write WhatsApp/phone replies for "72 Ghante", an assistant that helps an Indian farmer report crop loss
under PMFBY (crop insurance) within 72 hours. Write in the requested language and its native script, in simple
spoken village style, warm and respectful (use "aap"/"tumhi"/"aap sa" forms). 1-4 short sentences. No markdown.
Rules you must never break:
- Use ONLY the facts given. Never invent numbers, dates, names or promises.
- Never address the farmer by a name, and never use gendered words like dada/bhau/tai/saheb/sir/madam/bhai/behen;
  the farmer may be a man or a woman. Use neutral respectful address only (e.g. "Namaskar", "aapan/tumhi", "aap").
- Do not greet again in every message; greet only if facts.first_message is true.
- Never say a report is submitted/registered/done unless facts.status == "verified".
- If facts.status is "pending", clearly say it is NOT done yet.
- Never promise claim money, eligibility, damage percentage or amount.
- Ask at most ONE question per message.
- Write numbers in digits.
Return JSON {"text": "..."}"""

FALLBACK = {
    "ask_field": "Please tell me: {question}",
    "confirm_readback": "Please check: {summary}. Shall I submit this report? Reply YES or NO.",
    "filing_started": "Submitting your loss report now. I will confirm only after the portal acknowledges it.",
    "pending_not_done": "The portal is not responding. Your report is PENDING, NOT done yet. I am retrying and using another official route.",
    "fallback_helpline": "Meanwhile you can also call 14447 (free). {script}",
    "fallback_officer": "Your report {report_id} is with the {office}. I am tracking it until {tracking_until}.",
    "verified_done": "Done. Your loss report is registered and verified. Docket number: {docket}.",
    "photo_rejected": "This photo cannot be used ({problems}). Please {how_to_fix}.",
    "escalated": "Your case has been escalated to the {who}: {reason}.",
    "status_update": "Current status: {report_status}.",
}


async def compose(case: dict, key: str, facts: dict) -> str:
    if case.get("scripted"):
        # Deterministic test runs and admin-created cases use fixed templates (no model call).
        try:
            return FALLBACK[key].format(**facts)
        except KeyError:
            return f"[{key}] " + json.dumps(facts, ensure_ascii=False, default=str)[:300]
    language = LANGUAGES.get(case.get("language") or "hi", "Hindi")
    farmer_said = next((h["text"] for h in reversed(case.get("history", [])) if h["role"] == "farmer"), "")
    prompt = (f"Write ONLY in {language}, in its native script (Devanagari for Hindi, Marathi and Marwadi) even if the farmer typed in English letters, matching the dialect of the farmer's own words: \"{farmer_said[:300]}\"\n"
              f"The report is a crop-loss intimation (nuksan ki suchna), never call it a complaint.\n"
              f"Reply type: {key}\nFacts: {json.dumps(facts, ensure_ascii=False, default=str)}")
    try:
        data = await llm.generate_json(f"write_reply:{key}", SYSTEM, [prompt], case["id"])
        text = (data.get("text") or "").strip()
        if text:
            return text
    except llm.LLMUnavailable:
        pass
    template = FALLBACK.get(key, "{facts}")
    try:
        return template.format(**facts, facts=json.dumps(facts, ensure_ascii=False, default=str))
    except KeyError:
        store.add_trace(case["id"], "decision", "reply_fallback", "error", None, key, facts)
        return json.dumps(facts, ensure_ascii=False, default=str)
