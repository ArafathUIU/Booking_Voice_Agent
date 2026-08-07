# Jervis — Voice Agent for Dental Clinic Booking

Jervis is a **voice-based AI receptionist for a dental clinic**. It answers browser
calls, carries on a natural conversation in real time, collects booking details,
checks availability, holds a slot, confirms with the caller, and records the
booking — all through a real-time audio pipeline built on **LiveKit**, **Pipecat**,
**faster-whisper**, **Kokoro TTS**, and **Groq**. Every turn is persisted to
**PostgreSQL**, and confirmed appointments land in a dedicated `bookings` table.

This document describes **what is actually built** and **how it all fits together**
as of the current codebase.

---

## Architecture Overview

```
Caller (browser) ── WebRTC a/v ──▶ LiveKit (media/signaling)
                                      │
                                      ▼  (LiveKit Go SDK over ws/wss)
                          ┌─────────────────────────────────────────┐
                          │  Pipecat voice pipeline             │
                          │   LiveKit transport ──▶ Silero VAD
                          │        ──▶ faster-whisper STT
                          │        ──▶ ConversationManager (orchestrator)
                          │        ──▶ Kokoro TTS ──▶ transport   │
                          └───────────────────┬─────────────────────┘
                                              │  HTTP/async
                       ┌──────────────────────┴─────────────────────┐
                       ▼                                             ▼
              FastAPI  (backend)                              PostgreSQL (pgvector)
              - /api/v1/calls/room  → create room + spawn agent   call_sessions
              - /ws/transcript/{id} → live chat history over WS    bookings
              - serves static index.html (chat UI)                 ai_configs
                                                                   voice_configs
                                                                   knowledge_chunks
              Redis  (caching: conversation context, transcripts, slot hold)
```

### High-level flow

1. The browser requests a room: `POST /api/v1/calls/room`.
2. FastAPI creates a LiveKit room + tokens and a `call_sessions` row (`ringing`).
3. A `BackgroundTask` spawns the Pipecat agent worker for that room.
4. The browser joins and connects to `/ws/transcript/{session_id}` to render the
   live conversation in the chat UI.
5. The agent runs the FSM-style dialogue, checks availability, confirms, and books.
6. On a confirmed booking, a row is persisted to the `bookings` table.

---

## The Four Moving Parts (Docker Compose)

| Service | Image / Build | Container | Host Port | Purpose |
|---------|---------------|-----------|-----------|---------|
| `api` | `./backend` Dockerfile | `jervis-api` | 8001 → 8000 | FastAPI backend + Pipecat agent + chat UI |
| `db` | `pgvector/pgvector:pg16` | `jervis-db` | 5434 → 5432 | PostgreSQL + pgvector |
| `redis` | `redis:7-alpine` | `jervis-redis` | 6382 → 6379 | Session context / transcript cache, slot hold |
| `livekit` | `livekit/livekit-server:latest` | `jervis-livekit` | 7890/7891 TCP, 7882 UDP | WebRTC signaling + media (dev mode) |

There is also a standalone **pgAdmin** container (`dpage/pgadmin4`, port 5050) used
for browsing the database with a GUI.

> **Deployment note:** For production, the local `livekit` container is replaced by
> **LiveKit Cloud** (managed WebRTC), and the backend/database/redis move to a
> hosted platform such as **Railway**. See [Deployment](#deployment) below.

---

## Backend Layout (`backend/app/`)

```
app/
├── main.py                    FastAPI app factory, DB init, model warm-up, WS + static
├── config.py                  Pydantic settings (loaded from .env)
├── agents/                    The voice brain
│   ├── pipeline.py            Pipecat pipeline assembly (LiveKit transport, VAD, STT, TTS)
│   ├── worker.py              Agent worker entrypoint that runs the pipeline
│   ├── conversation_manager.py  Orchestrator: FSM decision → tool call → TTS → persist
│   ├── dialogue_manager.py    Deterministic dialogue policy (next-action decision + phrasing)
│   ├── conversation_state.py  Per-call state + slot extraction + date/time resolution
│   ├── tools.py               Availability, slot hold, and booking callables
│   ├── llm_service.py         Free-form / small-talk LLM (Groq) with RAG
│   ├── response_arbiter.py    Picks the winning response when multiple sources propose text
│   ├── confident_stt.py       Confidence-aware STT wrapper (repair gate)
│   ├── model_warmup.py        Pre-download/load Whisper + Kokoro at startup
│   ├── livekit_audio_patch.py Fixes a LiveKit SDK stereo→mono downmix bug
│   └── constants.py           Shared constants
├── api/v1/calls.py            Room creation endpoint + agent task lifecycle
├── models/                    SQLAlchemy models
├── services/                  call_service, knowledge_base, embeddings
├── utils/                     livekit_client, redis_client
├── static/index.html          Single-file chat UI (served by FastAPI)
└── websockets/                WebSocket connection manager (chat history push)
```

---

## Conversation Manager — How a Call Actually Works

`conversation_manager.py` is a Pipecat `FrameProcessor` sitting between STT and TTS.
Per user turn it:

1. **Repairs** — if ASR confidence is below the threshold and the utterance looks
   like noise, it asks for a repeat instead of proceeding.
2. **Extracts slots** — name, phone, service, date, and time are pulled from the
   utterance into `ConversationState` via `conversation_state.extract_*`.
3. **Decides** — `dialogue_manager.decide()` picks the next action *deterministically*
   (no LLM in the booking path).
4. **Executes** — the action runs: ask a question, check availability, hold a slot,
   book, answer from the knowledge base, or fall back to the LLM for small talk.
5. **Arbitrates** — `response_arbiter` chooses exactly one response.
6. **Speaks** — the reply is pushed as a `TTSSpeakFrame`.
7. **Persists** — the turn is queued to a background consumer that writes user/agent
   turns, stage, intent context, and the booking snapshot to the DB.

### Deterministic booking flow

```
name → phone → service → date → check availability
      → offer slots → choose slot → confirm → book → done
```

The LLM is **never** used to decide the next booking step — decisions come from a
fixed order + presence of extracted slots, so slot choice and booking stay reliable.

### Dialogue actions (`dm.*`)

| Action | Behaviour |
|--------|-----------|
| `ask_name` / `ask_phone` / `ask_service` / `ask_date` | Fixed-order slot gathering; re-asks vary wording |
| `check_availability` | Query business hours for open slots |
| `offer_slots` | Present up to 3 open times; wording varies on repeat |
| `choose_slot` | Caller picks a time; records `time_pref` + resolved datetime |
| `confirm_booking` | Restate name/service/date/time, ask to confirm |
| `confirm` / `cancel_confirm` | Confirm → book; cancel → re-offer |
| `answer_question` | Deterministic answer from the knowledge base |
| `clarify` | Restate the offer/confirmation when the caller asks for it again |
| `close` | Wrap up the call (honest wording — see below) |
| `respond` | Free-form small talk via the LLM |

### Recent reliability fixes (important behaviour)

The following behaviours were added/fixed as a result of reviewing live
transcripts and handling a confirmed booking call:

- **No repeated offers verbatim.** `offer_text` / `reoffer_text` track a count and
  vary the phrasing on repeats.
- **Honest sign-off.** `close_text` only says *"All set… confirmed"* if a booking
  actually exists (`booking_id` set); otherwise it politely offers to book later.
- **Real phone numbers only.** The phone threshold was raised from 4 to **7 digits**,
  so partial captures like `"01742"` no longer get accepted and the caller is asked again.
- **Clarification handling.** A new `clarify` action re-states the offered/confirmed
  times when the caller asks "which times again?" instead of forcing a new question.
- **No re-offer loop after goodbye.** Once the call is closed (`call_closed`), the
  agent stops restarting the slot-gathering flow; only a fresh booking intent reopens it.
- **Correct clinic timezone.** `clinic_timezone` defaults to `Asia/Dhaka`, and dates
  like "tomorrow" resolve to the correct weekday (e.g. Tuesday → Wednesday's date).
  The `tzdata` package ensures the tz database is available.
- **Booking `end_dt` bug fixed.** A previously-uninitialized `end_dt` caused the
  booking to fail; it is now derived from the resolved start datetime.

---

## Booking Persistence

Two tables track a completed call:

- **`call_sessions`** — the call itself: status (`ringing → in_call → ended`),
  stage (`fsm_state`), structured `transcript` (JSONB), and `booking_outcome`
  (e.g. `{"status":"confirmed","booking_id":"booking-2"}`).
- **`bookings`** — a flat, query-friendly record written whenever a call is confirmed:

  | Column | Type | Meaning |
  |--------|------|---------|
  | `id` | UUID | Primary key |
  | `session_id` | UUID | Which call session |
  | `customer_name` | String | Caller's name |
  | `customer_phone` | String | Caller's 7+ digit phone |
  | `slot` | String | Spoken slot, e.g. "11 AM" |
  | `booking_date` | timestamptz | Resolved appointment datetime |
  | `booking_confirmed` | Boolean | True when the caller said yes |
  | `created_at` / `updated_at` | timestamptz | Audit timestamps |

How it is written:
- `conversation_manager._booking_snapshot()` builds the row data **only** when
  `booking_outcome.status == "confirmed"`.
- `_enqueue_persist()` includes the snapshot in the background persistence payload.
- `_persist_booking()` upserts it into `bookings` (idempotent per session).
- `main.py` imports the `Booking` model so `Base.metadata.create_all` creates the table.

To inspect bookings:
```bash
docker exec jervis-db psql -U user -d voiceagent -c "SELECT * FROM bookings ORDER BY created_at DESC;"
```
Or open pgAdmin at **http://localhost:5050** (login `admin@example.com` / `admin`).

---

## Configuration (`app/config.py` + `.env`)

| Setting | Default | Description |
|---------|---------|-------------|
| `database_url` | `postgresql+asyncpg://user:pass@db:5432/voiceagent` | PostgreSQL (pgvector) |
| `redis_url` | `redis://redis:6379` | Redis cache |
| `livekit_url` | `http://livekit:7880` | LiveKit internal URL (backend) |
| `livekit_public_url` | `ws://localhost:7890` | URL the browser connects to |
| `livekit_api_key` / `livekit_api_secret` | `devkey` / `secret` | LiveKit auth |
| `groq_api_key` / `groq_model` | — / `llama-3.1-8b-instant` | Groq LLM (`llm_*` tuning below) |
| `stt_model` | `base` | faster-whisper model; `int8` compute on CPU |
| `tts_voice` | `af_heart` | Kokoro voice |
| `auto_warmup` | `true` | Pre-load STT/TTS models at startup |
| `clinic_timezone` | `Asia/Dhaka` | Clinic TZ for date/time resolution |
| `business_hours_start` / `_end` | `9` / `17` | Opening hours (24h) |
| `slot_duration_minutes` | `60` | Slot length |
| `backchannel_enabled` | `true` | plays "mhm/uh-huh" while the caller speaks |
| `asr_confidence_repair_threshold` | `-2.0` | ASR repair gate |
| `tenant_id` | `00000000-…-0001` | Default (single) tenant |

LLM tuning: `llm_max_tokens=256`, `llm_temperature=0.35`, `llm_history_turns=6`,
`llm_timeout_s=45.0`, `llm_max_retries=2`, `llm_max_tool_calls=4`.

---

## API Endpoints (what exists)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/calls/room` | Create a LiveKit room + tokens, create a session row, spawn the agent worker (body: `customer_name`, `customer_phone`) |
| `GET` | `/health` | Liveness probe |
| `WS` | `/ws/transcript/{session_id}` | Live transcript events pushed to the chat UI |
| `GET` | `/` | Serves the chat UI (`static/index.html`) |

> The broader set of REST endpoints from the earlier design docs (customers,
> appointments, calendar, etc.) are **not yet built** — they remain in the vision /
> roadmap docs, not this codebase.

---

## Database Tables

**Created by SQLAlchemy models (`main.py` → `Base.metadata.create_all`):**
- `call_sessions` — call lifecycle, transcript (JSONB), booking outcome
- `bookings` — confirmed appointment records (see above)
- `ai_configs` — per-tenant LLM/STT/TTS configuration
- `voice_configs` — per-tenant voice configuration
- `knowledge_chunks` & `conversation_summaries` — created by `init-vector-db.sql` (pgvector)
  during first DB boot, plus `CREATE EXTENSION IF NOT EXISTS vector`.

**Knowledge base tables** are seeded/queried by `app/services/knowledge_base.py`
(pgvector cosine similarity on `vector(384)` `all-MiniLM-L6-v2` embeddings) for
factual Q&A about the clinic.

---

## Frontend (`app/static/index.html`)

A single self-contained HTML file (vanilla JS + LiveKit JS SDK) served by FastAPI. It:

- Lets a caller **start a call** and shows **connecting / error / ended** states.
- Opens a LiveKit room and renders the **live chat transcript** streamed over
  `/ws/transcript/{session_id}` (history is pushed when the page joins).
- Shows a modern chat UI (header with status dot, user/agent bubbles, typing
  indicator, empty state).
- Has a **scrollable message area** (fixed earlier so messages no longer hide under
  the interface).
- Has **no text input** — this is a voice-only chat; the text area was removed.

---

## Running the Project Locally

### Prerequisites
- Docker & Docker Compose
- Python 3.11+ (for tests / local dev)
- A `GROQ_API_KEY` in `.env`

### Quick start
```bash
cd Jervis

# 1. Environment
copy .env.example .env        # then add GROQ_API_KEY, etc.

# 2. Start all services
docker compose up -d --build

# 3. (Optional) DB helper via pgAdmin
docker start pgadmin          # then open http://localhost:5050

# 4. Backend tests
cd backend
python -m pytest tests/ -q
```

### Docker Compose services
| Service | Port | Description |
|---------|------|-------------|
| `api` | 8001 | FastAPI + voice agent + chat UI |
| `db` | 5434 | PostgreSQL + pgvector |
| `redis` | 6382 | Redis cache |
| `livekit` | 7890/7891/7882 | WebRTC signaling + media (dev) |

Open **http://localhost:8001** in a browser and click **Start Call** to talk to Clara.

---

## Deployment

The current compose runs everything locally with the LiveKit **dev server**. For
production:

1. **LiveKit Cloud** replaces the local `livekit` container (managed WebRTC/UDP/TURN/TLS).
2. **Backend + Postgres + Redis** host on a platform like **Railway** (Docker-native,
   managed Postgres with pgvector, managed Redis, always-on service) — **Render** is a
   fallback; **Vercel** cannot run the backend.
3. Config changes required:
   - `livekit_url` / `livekit_public_url` → your LiveKit Cloud project `wss://` endpoint
   - real `livekit_api_key` / `livekit_api_secret`
   - `DATABASE_URL`, `REDIS_URL`, `GROQ_API_KEY` → managed endpoints/secrets
   - run `init-vector-db.sql` on the managed DB
   - give the API service ≥ 1 GB RAM (Whisper + Kokoro run on-device) and pre-download models
   - drop the `--dev` LiveKit flags for a secure production config

---

## Testing

Backend tests live in `backend/tests/`:

```bash
cd backend
python -m pytest tests/ -q     # currently 21 tests, all passing
```

They cover the conversation FSM without an event loop (DB persistence consumer is
disabled in the test harness): slot progression, phone extraction/validation,
confirmation booking, one-response-per-turn, question variation, closed-call
behaviour, clarification, and honest close wording. A real end-to-end voice call is
verified manually via the browser UI + the `call_sessions` and `bookings` tables.

---

## Key Design Decisions

- **Deterministic booking FSM.** The LLM is only used for free-form small talk — the
  booking path never relies on it, keeping slot selection and confirmation reliable.
- **One response per turn.** The agent speaks exactly once per utterance.
- **Background persistence.** DB writes run in a dedicated consumer outside Pipecat's
  frame context, so writes are never cancelled mid-turn.
- **Timezone-aware scheduling.** Dates/times resolve to tz-aware datetimes in the
  clinic's timezone (`Asia/Dhaka` by default).
- **Local inference.** Whisper (int8 CPU) + Kokoro ONNX run on-device — no STT/TTS API
  cost, pre-warmed at startup.
- **Chat history over WebSocket.** The UI isn't polling; it streams live transcript
  events.
- **Booking records in a flat table** so confirmed appointments are trivially
  queryable (beyond the JSONB `booking_outcome` on the session).

---

## Future Enhancements (not yet built)

- Google Calendar integration (real availability + bookings instead of generated slots)
- Multi-tenant RLS + auth (JWT) and the broader REST/admin API surface from the design docs
- Call summaries / NLP analytics / sentiment
- Telephony (SIP) inbound/outbound
- Outbound calls and SMS reminders

*Reference docs: `Jervis/Plan.md` and `Jervis/voice_agent_project_context.md` contain
the original vision/roadmap. This README reflects what is implemented in the code.*