"""Turns what the farmer said into structured fields (Gemini), then the agent checks them.

The LLM only extracts. It never decides deadlines, routes or whether something was filed.
"""

from . import clock, llm

LANGUAGES = {
    "mr": "Marathi", "hi": "Hindi", "mwr": "Marwadi (Rajasthani)", "gu": "Gujarati", "ta": "Tamil", "te": "Telugu",
    "kn": "Kannada", "bn": "Bengali", "pa": "Punjabi", "or": "Odia", "ml": "Malayalam", "en": "English",
}

PERILS = ["hailstorm", "landslide", "inundation", "cloudburst", "natural_fire", "lightning_fire",
          "cyclone", "cyclonic_rain", "unseasonal_rain", "drought", "dry_spell", "widespread_flood", "pest_attack", "disease", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["report_loss", "answer", "consent_yes", "consent_no", "correction", "status_query", "question", "skip", "greeting", "other"]},
        "language": {"type": "string"},
        "peril": {"type": ["string", "null"], "enum": [*PERILS, None]},
        "crop": {"type": ["string", "null"]},
        "crop_harvested": {"type": ["boolean", "null"]},
        "event_time_expression": {"type": ["string", "null"]},
        "event_time": {"type": ["string", "null"]},
        "event_time_confidence": {"type": "number"},
        "survey_no": {"type": ["string", "null"]},
        "application_no": {"type": ["string", "null"]},
        "area_acres": {"type": ["number", "null"]},
        "village": {"type": ["string", "null"]},
        "damage_extent": {"type": ["string", "null"]},
        "question_text": {"type": ["string", "null"]},
        "ticket_no": {"type": ["string", "null"]},
        "surveyor_visited": {"type": ["boolean", "null"]},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "summary_en": {"type": "string"},
    },
    "required": ["intent", "language", "event_time_confidence", "ambiguities", "summary_en"],
}

SYSTEM = """You are the understanding step of "72 Ghante", an agent that helps Indian farmers report crop loss under
PMFBY within 72 hours. The farmer speaks Marathi, Hindi, Marwadi or another Indian language, often code-mixed.
Extract ONLY what the farmer actually said in THIS message. Use null for anything not said. Never invent numbers.

Fields:
- intent: report_loss (describes damage), answer (answers the agent's question), consent_yes / consent_no
  (only when the agent is waiting for submit confirmation: "ho/haan/hoy/thik aahe/kar do" = yes; "nahi/naka/ruko" = no),
  correction (says something earlier was wrong), status_query, question, skip (declines to send photo / says skip), greeting, other.
- peril (English label): gaar/ole/olavrushti/hail = hailstorm; avkali paus/bemausam baarish = unseasonal_rain;
  dhagfuti/badal fatna = cloudburst; paani bharla/khet me paani bhar gaya = inundation; aag = natural_fire;
  vij padun aag = lightning_fire; bhuskhalan = landslide; vadal/chakrivadal = cyclone; dushkal/sukha = drought;
  kid/alli/sundi = pest_attack.
- crop: English common name (kanda/pyaaj = onion, soyabin = soybean, draksh/angoor = grapes, bajri/bajra = bajra,
  makka = maize, kapus = cotton, jeera = cumin, gahu = wheat, harbhara/chana = chickpea, tamatar = tomato).
- crop_harvested: true if the crop was already cut/harvested and lying in the field; false if standing; null if not said.
- event_time: resolve the spoken time against NOW into ISO 8601 with +05:30. "kal raat" = yesterday ~22:00,
  "aaj pahate/subah" = today ~05:00/07:00, "parva/parso" = 2 days ago, "kal dupari" = yesterday ~14:00, "kal sandhyakali/shaam" = yesterday ~18:00.
  When the time of day is unclear, choose the EARLIER plausible time (safer for the 72-hour deadline) and lower the confidence.
- event_time_confidence: 0..1.
- survey_no / gat no: as spoken, digits and slash only, e.g. "112/2". application_no: as spoken.
- language: mr, hi, mwr (Marwadi/Rajasthani: "mharo, thane, kai, ghano, padyo"), gu, ta, te, kn, bn, pa, or, ml, en.
- ticket_no: a helpline ticket / complaint number the farmer reads out, else null.
- surveyor_visited: true if the farmer says the surveyor/officer came and inspected the field; false if they say he did not come.
- ambiguities: short English notes on anything unclear or contradictory.
- summary_en: one-line English summary."""


async def understand(text: str, context: dict, case_id: str) -> dict:
    now = clock.now()
    prompt = (
        f"NOW: {now:%A %Y-%m-%d %H:%M} IST\n"
        f"Agent is currently waiting for: {context.get('pending_question') or 'nothing specific'}\n"
        f"Agent is waiting for submit confirmation: {context.get('awaiting_consent', False)}\n"
        f"Known so far: {context.get('fields')}\n\n"
        f"Farmer's message:\n{text}"
    )
    return await llm.generate_json("understand_message", SYSTEM, [prompt], case_id, SCHEMA)
