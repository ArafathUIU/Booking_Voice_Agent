# Jervis — Outbound Telephony (AI Calls You)

Design + implementation guide for adding **telephony to Jervis**, outbound first:
a user fills a form → the AI agent automatically calls them → on success the booking
lands in `bookings`; on no-answer the lead is retried later.

---

## 1. Corrections to your original plan (read this first)

Your plan is fundamentally right. Four refinements make it more robust:

| Your idea | Refinement | Why |
|---|---|---|
| 1. Form → save to `leads` table | ✅ Keep exactly this | `leads` is the source of truth for "who to call + what about". |
| 2. "New/unanswered phone" | Give `leads` a **status** (`pending → dialing → completed / no_answer / failed`) and **retry fields** (`attempt_count`, `last_dialed_at`, `next_retry_at`) | "New" and "unanswered" are two *states of the same row*, not two rows/tables. |
| 3. "Store unanswered in a **different** table" | Use a child **`call_attempts`** log table for each dial, while `leads` stays the single source of truth | One `leads` row per person = easy retry logic; the log keeps history/audit. You *can* call it a separate table — it is — it just records attempts, not the lead. |
| 4. "The **something** (no Celery beat)" | An **APScheduler `AsyncIOScheduler` poller running in the FastAPI process**, atomic-claiming pending rows via `UPDATE … WHERE status='pending' RETURNING id` | No Celery; poller picks up **new and no-answer** leads; the atomic claim guarantees a lead is never dialed twice. |

Other important corrections not in your list:
- **Seed the agent with lead context.** Pass `name` + `purpose` from the lead into the
  agent so it greets by name and doesn't re-ask name/phone. The FSM then starts at
  purpose/service instead of "your name please".
- **Agent joins the room BEFORE the phone rings**, so the caller never hears silence.
- **Booking → `bookings`; outcome also echoed onto `leads`** so the dashboard reads one table.

---

## 2. The flow (end to end)

```
[User] fills form (name, phone, purpose)
   │  POST /api/v1/leads
   ▼
┌─────────────────────────────────────────────┐
│  leads  (status = pending)                  │
│  id | tenant_id | name | phone | purpose    │
│     | status | attempt_count | next_retry_at│
└───────────────┬─────────────────────────────┘
                │  APScheduler poller (every ~5s)
                ▼  atomic claim: pending → dialing
┌─────────────────────────────────────────────┐
│  Outbound orchestrator (api/v1/calls)       │
│  1. create LiveKit room                     │
│  2. create call_sessions (status=dialing)   │
│  3. spawn Pipecat agent (seeded: name+purpose)
│  4. wait until agent participant is in room │
│  5. SipService.create_sip_participant(trunk, to=+880…)
└───────────────┬─────────────────────────────┘
                ▼ Twilio trunk → LiveKit SIP → ring
        ┌───────────────┴───────────────┐
        ▼                              ▼
   User answers                  No answer / busy / fail
   (bridged into room)           recorded in call_attempts
        │                        leads.status = no_answer
        ▼                        next_retry_at = +N minutes
   Agent: "Hi {name}, this is
   Clara from the clinic,
   calling about {purpose}…"
        │  booking flow
        ├───────────────┐
        ▼               ▼
   books               declines
   └──▶ bookings row   └──▶ leads.status=completed (no booking)
        leads.status = completed
```

---

## 3. Architecture

### The best possible way (current): **Twilio Programmable Voice + Media Streams**

**Twilio trial blocks Elastic SIP Trunks and SIP Domains** (verified live), so the
LiveKit SIP path can't be used until the account is upgraded. But trial
Programmable Voice quick calls **do** work, so outbound dialing now uses Twilio's
REST API directly, and call audio is bridged back over a **Media Streams
WebSocket** into the same ConversationManager pipeline. No LiveKit room needed
for outbound.

```
Phone
   │  Twilio REST Calls.create (TwiML: <Connect><Stream url="wss://…/ws/twilio-media">)
   ▼
Twilio Programmable Voice ─── PSTN ring ───▶ callee
   │  (answered) opens Media Stream WS
   ▼
Pipecat FastAPIWebsocketTransport + TwilioFrameSerializer (8k ulaw <-> 16k PCM)
   │  VAD → STT → ConversationManager → TTS
   ▼
FastAPI backend (main.py) ── status webhook /webhooks/twilio/status
```

| Option | Fit for this codebase | Verdict |
|---|---|---|
| **Twilio Programmable Voice + Media Streams** | Reuses ConversationManager + STT/TTS via a new `FastAPIWebsocketTransport` pipeline flavor; works on trial | ✅ **Current** |
| LiveKit SIP + Twilio trunk | Reuses the LiveKit room flow directly | ❌ Blocked on trial (needs upgrade); kept in code for future |
| Twilio Media Streams (direct) | This is what we now do | ✅ |

### No-Celery "something": APScheduler poller

A single `AsyncIOScheduler` starts in `main.py` lifespan. Every ~5 seconds it:

1. Runs `SELECT … FROM leads WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at <= now)`.
2. **Atomically claims** one row: `UPDATE leads SET status='dialing', attempt_count=attempt_count+1, last_dialed_at=now WHERE id=<id> AND status='pending' RETURNING id` — the `AND status='pending'` guard means only one worker ever claims it.
3. Hands the claimed lead to the outbound orchestrator (in-process). There's no distributed queue; the single process is the only dialer, which is fine for a demo and avoids double-calls.

`no_answer` leads are re-armed by setting `status='pending'` + `next_retry_at`, so the *same* poller naturally retries them later.

---

## 4. Data model

### `leads` (the person to call — source of truth)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID | single default tenant |
| `customer_name` | String | from form |
| `phone` | String | E.164, e.g. `+8801…` |
| `purpose` | String | free text from form |
| `service` | String nullable | mapped from purpose (cleaning/checkup/whitening) |
| `status` | String | `pending → dialing → completed / no_answer / failed` |
| `attempt_count` | Int | dials so far |
| `last_dialed_at` | timestamptz | last dial time |
| `next_retry_at` | timestamptz | when it can be dialed again |
| `call_session_id` | UUID nullable | last session for this lead |
| `created_at` / `updated_at` | timestamptz | |

### `call_attempts` (each dial — the "different table" you asked about)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `lead_id` | UUID FK | which lead |
| `call_session_id` | UUID nullable | LiveKit session |
| `outcome` | String | `answered / no_answer / busy / failed` |
| `details` | String nullable | SIP status code / message |
| `created_at` | timestamptz | |

The `bookings` table (existing) stores successful appointments as before.

---

## 5. Backend changes (files)

| File | Change |
|---|---|
| `app/models/lead.py` | **NEW** — `leads` table |
| `app/models/lead_attempt.py` | **NEW** — `call_attempts` table |
| `app/models/__init__.py` | register new models |
| `app/services/lead_service.py` | **NEW** — create lead, atomic claim (`UPDATE … WHERE status='pending'`), record attempt, re-arm retry |
| `app/api/v1/leads.py` | **NEW** — `POST /api/v1/leads` (form), `GET /api/v1/leads` (list/status) |
| `app/scheduler/jobs.py` | **NEW** — `AsyncIOScheduler` poller → outbound flow |
| `app/utils/sip_client.py` | **NEW** — wrap `SipService`: idempotent outbound trunk + `dial_outbound(room, to, from)` |
| `app/api/v1/calls.py` | add `POST /outbound` (create room+session, spawn seeded agent, wait agent-ready, SIP dial); `POST /calls/{id}/end` |
| `app/services/call_service.py` | support `session_type="phone_outbound"`, status `dialing` |
| `app/config.py` | `SIP_TRUNK_ID`, `SIP_FROM_NUMBER`, `SIP_DIAL_TIMEOUT`, `SIP_POLL_AGENT_TIMEOUT`, `TWILIO_*` |
| `app/agents/worker.py` + `pipeline.py` + `conversation_manager.py` | accept + seed `lead_context` (name, purpose→service); outbound greeting |
| `app/main.py` | register `leads` router, start scheduler in lifespam, add `/webhooks/livekit/sip` |
| `app/static/index.html` | **NEW** "Request a call" form view (name, phone, purpose) + status feedback |
| `docker-compose.yml` | add `livekit-sip` sidecar + enable SIP in local LiveKit config |

No change needed to `bookings` persistence — it already writes correctly for any
transport.

---

## 6. Lifecycle & retry

```
pending ──(claimed by poller)──▶ dialing
                                  │
                 ┌────────────────┼────────────────┐
                 ▼                ▼                ▼
            answered          no_answer         failed
             │                 │  (record          │
             │                 │   attempt)        │
             ▼                 ▼                   ▼
   booking flow          re-arm for retry    re-arm or flag
        │                (pending,            (pending, longer
        ├─ booked ─▶ bookings + completed     backoff)
        └─ not booked ─▶ completed
```

- **Retry policy** (configurable): no_answer → retry in `+10 min`, `attempt_count` cap
  (e.g. 3), then mark `failed`. Failed rows are surfaced in the UI and can be re-armed manually.
- **Agent joins before dialing** so the person never hears dead air.
- **Webhook** (`/webhooks/livekit/sip`) updates session + lead status when the call
  actually connects / ends / times out.

---

## 7. Local dev vs production

| | Local (dev) | Production |
|---|---|---|
| LiveKit SIP | `livekit/livekit-sip` sidecar in compose | **LiveKit Cloud** (SIP built-in) |
| PSTN trunk | Twilio, real number (may dial +880) | Twilio, BD-capable number |
| URLs | `ws://localhost:7890` | `wss://<cloud-project>.livekit.cloud` |
| Seeding | n/a | pre-download Whisper/Kokoro; ≥1 GB RAM |

---

## 8. Roadmap

Status as of the current build:

- [x] **Models + form intake** — `leads`, `call_attempts`, `POST /api/v1/leads`, form UI.
- [x] **Scheduler + outbound** — poller, atomic claim, `POST /outbound`, retry/re-arm, attempt logging.
- [x] **Agent seeding + greeting** — name + purpose into `ConversationState`; outbound greeting.
- [x] **Twilio switch** — `twilio_client.py` (REST `Calls.create`), `twilio_ws.py`
      (Media Streams WS), `webhooks.py` (status callback), `twilio_pipeline.py`
      (FastAPIWebsocketTransport flavor reusing ConversationManager).
- [ ] **Live verification** — set `TWILIO_AUTH_TOKEN` + `TWILIO_PUBLIC_BASE_URL`
      (ngrok), dial `+8801774604502`, confirm Media Streams is allowed on trial
      (docs conflict; only a real call settles it).
- [ ] **Status UI** — lead/attempt state surfaced on the dashboard (data model done).

---

## 9. Caveats / responsibilities

- **Bangladesh outbound (+880)** via Twilio: cost/quality vary by carrier; some BD
  numbers are flagged or regulated. Verify Twilio supports your destination before launch.
- **Consent**: calling users who submitted the form implies consent for booking-related
  contact, but confirm local telecom/regulatory obligations.
- Form is not gated (anyone can submit) — add simple rate-limiting later if needed.

---

## 10. Quick reference — what I'll use from your code

- `app/api/v1/calls.py` `create_call_room` → template for the outbound orchestrator.
- `app/agents/worker.py` / `pipeline.py` `run_agent_worker` → spawn the seeded agent.
- `app/agents/conversation_manager.py` `ConversationManager(session_id, tenant_id)` →
  add a `lead_context` seed parameter.
- `app/services/call_service.py` `create_session(..., session_type)` → add `phone_outbound`.
- `app/utils/livekit_client.py` `LiveKitAPI` + installed `SipService.create_sip_participant` →
  the dial call.
- `app/config.py` Settings + `.env` → new telephony settings.