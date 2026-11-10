# 72 Ghante

**A WhatsApp agent that helps an insured farmer file a crop-loss claim inside the 72-hour window.**

National Finalist — SARCathon 2026, SARC, IIT Bombay.

---

## The problem

Under PMFBY (Pradhan Mantri Fasal Bima Yojana), a farmer hit by a localised calamity — hail, lightning fire, landslide, inundation — has **72 hours** to intimate the insurer. Miss the window and the claim is usually gone, no matter how real the damage.

That window is hard to meet. The farmer may not know the clock exists, may not have the policy slip to hand, may not read English, and the official filing routes fail quietly and often.

## What this does

The farmer sends a voice note on WhatsApp in their own language. The agent:

1. **Starts the clock.** It works out the loss time from what was said and computes the exact deadline.
2. **Rebuilds the policy** from a photo of the insurance slip or passbook, instead of asking the farmer to type numbers.
3. **Checks the photos** of the damaged crop before they are submitted.
4. **Reads the case back** to the farmer, in their language, and waits for consent.
5. **Files it**, trying the official routes in order and switching when one fails.
6. **Verifies** that a docket number actually came back — it never reports "done" without one.
7. **Tracks the hand-off** to the surveyor and escalates if nothing moves.

Everything the agent does is written to a trace you can open and read, step by step.

## The deadline logic is not the LLM's job

The model extracts what the farmer said. The rules about deadlines are plain, deterministic Python (`app/deadline.py`):

| Loss type | Regime | Window |
|---|---|---|
| Hailstorm, landslide, inundation, cloudburst, lightning or natural fire | Localised calamity | **72 hours** |
| Cyclone, cyclonic rain, unseasonal rain | Post-harvest | **14 days** |
| Drought, dry spell, widespread flood, pest attack, disease | Widespread | Area-based assessment, no individual intimation |

A language model that hallucinates a deadline costs a farmer their claim. So it never computes one.

## Does it actually work better than just using a chatbot?

Measured, not claimed. `eval/run_eval.py` runs **45 scripted farmer conversations** — 21 Hindi, 15 Marathi, 9 Marwadi — against this agent and against a plain chatbot using **the same AI models** with no tools. Scoring is automatic.

| | **72 Ghante** | Plain chatbot |
|---|---|---|
| Task completed | **100%** | 11% |
| Correct deadline | **100%** | 31% |
| Correct fields extracted | **100%** | 100% |
| Recovered from a failure | **100%** | 0% |
| Falsely said "done" | **0** | 16 |
| Avg tool calls | 3.2 | 4.0 |
| Avg time per case | 15.1s | 10.0s |

The row that matters is the last one in bold: the plain chatbot told the farmer the claim was filed **16 times when it had not been**. This agent did that zero times, because filing is only reported after a docket number comes back.

Full results: [`eval/results/summary.json`](eval/results/summary.json).

## How it is built

```
app/            FastAPI service
  agent.py        the loop: understand -> decide -> act -> verify
  understand.py   speech and text -> structured case fields
  deadline.py     the 72-hour / 14-day / area-based rules (no LLM)
  clock.py        case timers and urgency
  workflow.py     stage machine for a case
  routes.py       the official filing routes, in order, with fallback
  tools.py        the actions the agent is allowed to take
  channels.py     WhatsApp, SMS and voice in and out
  store.py        SQLite persistence
  stats.py        numbers behind the dashboard
mock_pmfby/     a stand-in PMFBY filing server, with a switch to make it fail
dashboard/      operator dashboard, live trace viewer, and a WhatsApp simulator
eval/           the evaluation harness, demo seeding, map tile prefetch
tests/          core tests
```

**Stack:** Python, FastAPI, SQLite, Gemini (with fallback models) for understanding and speech, Twilio and the Meta WhatsApp Cloud API for messaging, Leaflet for the maps.

## Channels

- **WhatsApp** — Twilio sandbox (`POST /whatsapp`) or the Meta Cloud API (`/meta/webhook`)
- **SMS** — `POST /sms`
- **Voice call** — `POST /voice/incoming`, for farmers who would rather speak than type
- **Built-in simulator** — `dashboard/phone.html`, a WhatsApp-style phone that drives the same agent with no telecom account at all

The simulator exists because a demo should not depend on a telecom sandbox being awake. It runs the identical code path.

## Running it

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY; Twilio keys are optional
python -m uvicorn app.main:app --port 8000
```

Then open:

- `http://127.0.0.1:8000/static/new.html` — operator dashboard
- `http://127.0.0.1:8000/static/phone.html` — farmer's phone simulator

Seed demo cases with `python -m eval.seed_demo`, and run the evaluation with `python -m eval.run_eval`.

The PMFBY endpoint defaults to the bundled mock server at `/mock`, which has a switch to make filing fail on demand — that is how the recovery behaviour is demonstrated.

## Honest scope

- The filing target is a **mock PMFBY server**, not the live government system. No public API exists for this.
- The evaluation uses **scripted** farmer conversations, not recordings of real farmers.
- WhatsApp runs through a sandbox or the simulator; it is not a production business account.

Everything else — the clock, the routes, the retries, the verification, the traces, the dashboard — is real code doing real work.

---

Built by [Yuvraj Daga](https://yuvrajdaga.com) for SARCathon 2026, SARC, IIT Bombay.
