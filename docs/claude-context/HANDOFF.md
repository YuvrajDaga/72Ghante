# 72 Ghante: handoff for Claude on the PC

Read this first, then `sarcathon-2026-finale.md` and `yuvraj-do-only-what-asked.md` in this folder. Copy those two into your own Claude memory if you want them remembered.

## Who and what
- **Yuvraj Daga**, solo team, SARCathon 2026 (IIT Bombay, Agentic AI Hackathon) finalist.
- **Finale:** 25 Oct 2026, offline at IIT Bombay (Alumination 2026). Format: working prototype, then pitch, then Q&A. The pitch time limit is unknown (ask SARC).
- **Judging:** 30% task completion & reliability, 25% real-world fit, 20% technical depth, 15% evaluation rigor, 10% pitch.
- **Project:** "72 Ghante", a WhatsApp/voice agent that helps a PMFBY-insured farmer file a **verified** crop-loss intimation inside the 72-hour window, then tracks the case until the surveyor visits. The Round 1 PDF and the problem statement are in the user's Downloads.
- **Main story:** Nashik (Marathi) farmer. **Second story:** Jodhpur (Marwadi). It also works in Hindi and other Indian languages.
- **Pitch hook (real case):** in Osmanabad, the insurer paid only the 72,325 farmers who reported within 72 hours. Everyone else was rejected (Bombay HC case).
- **Similar projects to differentiate from:** Fasal Rakshak ("The 72-Hour Clock", AWS, Sep 2026) and BimaSathi (AI for Bharat, Jun 2026). **Our difference:** we own the case after filing (verify the receipt, retry, switch route, track the hand-off, escalate), on WhatsApp voice.

## How Yuvraj wants to work (important)
- **Short replies, simple English, act fast.** He gets very frustrated by long thinking or long waits.
- Do only what he asks. English only.
- Show a screenshot after every visual change. Never claim a visual is done without checking it.
- His browser viewport is about **1536×830** (1920×1080 screen at 125% Windows scaling). Test layouts at that size.
- For design he sends ChatGPT-made images. Match them closely. Ask him before styling a visual part.
- **Keep the sidebar and top-banner background images as they are** (`dashboard/img/side_v2.jpg`, `dashboard/img/hero_v2.jpg`).

## Stack and run
- Python FastAPI + SQLite + Gemini (google-genai), with a plain HTML dashboard (Chart.js, Leaflet).
- **Setup:**
  ```
  py -m venv .venv
  .venv\Scripts\python -m pip install fastapi "uvicorn[standard]" httpx python-dotenv pillow imageio-ffmpeg python-multipart google-genai pytest
  ```
- Copy `.env.example` to `.env` and put `GEMINI_API_KEY` in it. The key is **not** in git; Yuvraj has it in Google AI Studio (key name "72ghante").
- **Seed demo data:** `.venv\Scripts\python -m eval.seed_demo` (842 synthetic cases)
- **Run:** `.venv\Scripts\python -m uvicorn app.main:app --port 8000`, then open http://localhost:8000
- **Tests:** `.venv\Scripts\python -m pytest tests -q`. **26 passed** at handoff.

## Code map
- `app/agent.py`: the agent loop. Perceive (speech/photo) → understand (Gemini) → plan → act → verify. It also has the background tick (retries, hand-off clock, surveyor tracking, escalation).
- `app/deadline.py`: 72-hour maths and urgency tiers (deterministic). `app/workflow.py`: status-transition rules. `app/routes.py`: route planner.
- `app/casesvc.py`: admin case create/edit, consent, submit, documents, and the 4 deterministic simulation scenarios.
- `app/api_cases.py`: admin API. `app/stats.py`: dashboard aggregates. `app/main.py`: webhooks (WhatsApp/voice/SMS via Twilio) and the other APIs.
- `mock_pmfby/server.py`: **mock** PMFBY (policy lookup, submit, status, duplicates, officer route, fault injection).
- `dashboard/index.html`: control room, all 13 sidebar pages. The CSS has many override blocks (v4–v9); the last block wins.
- `tests/test_core.py`, `eval/seed_demo.py`, `eval/smoke.py`.

## What works (verified)
- Marathi and Marwadi conversations end to end: policy found by phone number, 72-hour clock, consent, filing, verified docket.
- Portal down → "pending, not done" → helpline fallback → background recovery → verified.
- Simulations: success, timeout, server failure, invalid document. **All pass.**
- Duplicate filings are filed only once. Editing a case clears consent. No false "done".
- Data separation: live, demo (`CG-2026-*`) and test-run cases are kept apart. Test runs are excluded from analytics and shown only on the Reports page.

## Open / next
1. **Dashboard visuals.** Target image: `Glassmorphism Agriculture Dashboard UI Kit (1).png` in Downloads. The last agent pass ("v9": Hindi bubble on hero, gradient 72h ring, map pills + vertical legend, glow) was **stopped mid-edit**, so the layout at 1536×830 may be broken: squashed trend chart, overlapping crop icons, tiny map, scrollbars, clipped bottom row. Fix carefully at 1536×830, with screenshots.
2. **Real WhatsApp:** needs a Twilio account (sandbox) and ngrok. Put the keys in `.env` and set `PUBLIC_BASE_URL`.
3. **Gemini billing:** the free tier hits 429 errors quickly. Enable billing before the demo.
4. **Evaluation:** about 40 test cases, our agent vs plain Gemini (no tools). The results JSON goes to `eval/results/summary.json`, and the Model Evaluation page reads it.
5. **Real farmer voice notes:** Yuvraj is collecting 3–5. Add them to the evaluation.
6. **Still to make:** PPT, 7-minute and 3-minute speaking scripts, a Q&A sheet (30+ questions), and an AI-video script plus a backup demo video.
