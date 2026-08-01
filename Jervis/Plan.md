
content = """# Production Multi-Tenant AI Voice Agent SaaS
## Complete Project Context for AI Coding Assistant

---

## 1. Project Vision

Build a **production-grade, multi-tenant voice agent SaaS** that handles both **inbound and outbound web-based calls**. The agent converses naturally with users, answers questions about availability, books appointments into Google Calendar, and handles complex edge cases like race conditions, call drops, and interruptions.

### Core Scenario
1. A client fills a web form (name, email, phone, address, purpose) and submits it.
2. The agent initiates an **outbound web call** to the client.
3. The agent greets the client by name, introduces itself, and starts a natural conversation.
4. The agent asks follow-up questions and answers user questions using a knowledge base.
5. The user can query the schedule; the agent reads real-time availability.
6. The user selects a slot; the agent holds it tentatively, confirms, then books it atomically.
7. The user cannot book an already-booked slot. The agent cannot double-book.
8. The system also supports **inbound calls** (users calling the agent via web).

### Key Constraints
- **Multi-tenant**: Shared PostgreSQL schema with Row-Level Security (RLS)
- **Web calls only** (for now): No telephony. Browser-based via WebRTC.
- **Free stack**: Self-hosted / open-source where possible.
- **Celery-free**: Use FastAPI BackgroundTasks + APScheduler instead of Celery.
- **Production-grade**: Handle race conditions, call drops, barge-ins, calendar API failures.

---

## 2. Architecture Stack

| Layer | Tool | Purpose | Cost |
|-------|------|---------|------|
| **Voice Framework** | **Pipecat** (v1.0+) | Pipeline-of-processors model for voice agents. One-line provider swaps. Best Python DX for custom orchestration. | Free (Apache 2.0) |
| **WebRTC Transport** | **LiveKit** (self-hosted) | Open-source SFU. Sub-50ms latency. Handles browser calls. | Free |
| **STT** | **faster-whisper** (self-hosted) or Deepgram free tier | Speech-to-text. Whisper is fully self-hostable. | Free |
| **LLM** | **Llama 3.x** via Ollama/vLLM or Groq free tier | Conversation intelligence. Llama for privacy; Groq for speed. | Free |
| **TTS** | **Kokoro** (Apache 2.0) | ~75ms latency, lowest resource usage, commercial-use free. | Free |
| **VAD** | **Silero VAD** | Barge-in detection, interrupt handling. | Free |
| **Backend API** | **FastAPI** (Python) | Async API, BackgroundTasks, dependency injection. | Free |
| **Scheduler** | **APScheduler** (AsyncIOScheduler) | Periodic jobs: calendar sync, stale hold cleanup, reminders, retries. | Free |
| **Database** | **PostgreSQL 15+** | Multi-tenant data with RLS. Primary + read replicas. | Free |
| **Cache/State** | **Redis 7+** | Session state, distributed slot locks, freebusy cache. NOT a Celery broker. | Free |
| **Calendar** | **Google Calendar API v3** | freebusy.query, events.insert, ETag optimistic concurrency. | Free tier |
| **Task Tracking** | **PostgreSQL `job_logs` table** | Replaces Celery task result backend. | Free |
| **Frontend** | **React** | Web call interface, form submission, admin dashboard. | Free |

### Why Pipecat over LiveKit Agents?
LiveKit Agents bundles transport + agent logic — great for simple demos but opinionated. Pipecat gives pure pipeline control you drop on top of LiveKit's transport. You get LiveKit's media plumbing + Pipecat's flexible orchestration. For multi-tenant booking with a 10-state FSM, Pipecat is the safer production path.

### Why Celery-Free?
Celery adds 3 moving parts (broker, workers, beat) that aren't needed:
- Fire-and-forget tasks (SMS, spawn call) → FastAPI `BackgroundTasks`
- Periodic tasks (sync, cleanup, reminders) → APScheduler
- The heavy work (STT/LLM/TTS) happens inside stateful Pipecat workers, not background jobs

---

## 3. Multi-Tenant Architecture

### Pattern: Shared Schema with `tenant_id`
Every table has a `tenant_id` column. Row-Level Security (RLS) policies enforce filtering.

### Tenant Context Propagation
```
API Gateway extracts tenant from JWT or subdomain
    ↓
Sets app.current_tenant_id on every DB connection (transaction-scoped)
    ↓
All queries automatically filtered by RLS
    ↓
Redis keys namespaced: slot:{tenant_id}:{slot_id}
```

### RLS Policy Template
```sql
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::UUID);
```

Apply to: `users`, `customers`, `leads`, `services`, `slots`, `appointments`, `business_hours`, `calendar_integrations`, `ai_configs`, `voice_configs`, `knowledge_bases`, `knowledge_documents`, `call_sessions`, `notifications`, `job_logs`, `integrations`.

`audit_logs`: Optionally skip RLS for cross-tenant admin views, or apply with superuser bypass.

---


## 5. API Endpoints

### 5.1 Authentication & Tenant Management

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/auth/register` | Register a new tenant + admin user |
| POST | `/auth/login` | Login, returns JWT with tenant_id |
| POST | `/auth/refresh` | Refresh access token |
| POST | `/auth/logout` | Invalidate token |
| GET | `/auth/me` | Current user profile |
| GET | `/tenants/me` | Get current tenant details |
| PUT | `/tenants/me` | Update tenant profile (name, timezone, agent_name, etc.) |
| PUT | `/tenants/me/settings` | Update tenant_settings (slot_duration, notice_hours, etc.) |

### 5.2 Users (Admin Dashboard)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/users` | List users in tenant |
| POST | `/users` | Create user (admin only) |
| GET | `/users/{id}` | Get user details |
| PUT | `/users/{id}` | Update user |
| DELETE | `/users/{id}` | Soft delete user |

### 5.3 Customers

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/customers` | List customers (with pagination, search) |
| POST | `/customers` | Create customer (from form or admin) |
| GET | `/customers/{id}` | Get customer details |
| PUT | `/customers/{id}` | Update customer |
| DELETE | `/customers/{id}` | Soft delete customer |
| GET | `/customers/{id}/history` | Call + booking history for customer |
| GET | `/customers/lookup` | Lookup by phone or email (for inbound calls) |

### 5.4 Leads

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/leads` | List leads |
| POST | `/leads` | Create lead (from web form) |
| GET | `/leads/{id}` | Get lead details |
| PUT | `/leads/{id}` | Update lead status/priority |
| POST | `/leads/{id}/convert` | Convert lead to customer + create appointment |
| DELETE | `/leads/{id}` | Soft delete lead |

### 5.5 Services

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/services` | List active services |
| POST | `/services` | Create service |
| GET | `/services/{id}` | Get service details |
| PUT | `/services/{id}` | Update service |
| DELETE | `/services/{id}` | Soft delete service |

### 5.6 Slots & Availability (The Booking Core)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/availability` | Get available slots for a date range. Query params: `start_date`, `end_date`, `service_id` |
| POST | `/slots/hold` | Tentatively hold a slot. Returns hold_token. Body: `slot_id`, `customer_id`, `session_id` (optional) |
| POST | `/slots/release` | Release a held slot. Body: `slot_id`, `hold_token` |
| GET | `/slots/{id}` | Get slot details |

### 5.7 Appointments

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/appointments` | List appointments (with filters: date range, customer, status) |
| POST | `/appointments` | **Book an appointment**. This is the atomic booking endpoint. Body: `slot_id`, `customer_id`, `service_id`, `session_id` (optional), `notes` |
| GET | `/appointments/{id}` | Get appointment details |
| PUT | `/appointments/{id}` | Update appointment (notes, status) |
| DELETE | `/appointments/{id}` | Cancel appointment (soft delete + delete from Google Calendar) |
| POST | `/appointments/{id}/reschedule` | Reschedule to a new slot. Atomic: release old, hold new, book new |
| GET | `/appointments/{id}/sync-status` | Check if appointment is synced to Google Calendar |

### 5.8 Call Sessions

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/calls` | List call sessions |
| POST | `/calls/outbound` | **Trigger outbound call**. Creates customer/lead if new, creates LiveKit room, spawns agent worker via BackgroundTask. Body: customer data or customer_id |
| POST | `/calls/inbound` | **Handle inbound call**. Webhook from LiveKit. Looks up customer by phone, creates session, spawns agent worker |
| GET | `/calls/{id}` | Get call session details |
| GET | `/calls/{id}/transcript` | Get structured transcript |
| POST | `/calls/{id}/end` | End call session. Updates status, duration, ended_reason |
| POST | `/calls/{id}/escalate` | Mark call as escalated to human |
| GET | `/calls/{id}/recording` | Get call recording URL (if enabled) |

### 5.9 Calendar Integration

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/calendar/auth` | Get Google OAuth URL for tenant |
| POST | `/calendar/callback` | OAuth callback handler. Stores tokens |
| GET | `/calendar/status` | Check calendar connection status |
| POST | `/calendar/sync` | Trigger manual sync (BackgroundTask) |
| GET | `/calendar/events` | List calendar events (proxy to Google API) |
| DELETE | `/calendar` | Disconnect calendar (soft delete integration) |
| POST | `/calendar/webhook` | Google Calendar push notification webhook |

### 5.10 AI & Voice Configuration

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/ai-config` | Get AI config for tenant |
| PUT | `/ai-config` | Update AI config (system_prompt, model, temperature, provider_config) |
| GET | `/voice-config` | Get voice config for tenant |
| PUT | `/voice-config` | Update voice config (voice_id, language, stability, etc.) |
| POST | `/ai-config/test` | Test the AI config with a sample prompt |

### 5.11 Knowledge Base

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/knowledge-bases` | List knowledge bases |
| POST | `/knowledge-bases` | Create knowledge base |
| GET | `/knowledge-bases/{id}` | Get KB details |
| DELETE | `/knowledge-bases/{id}` | Soft delete KB |
| POST | `/knowledge-bases/{id}/documents` | Upload document |
| GET | `/knowledge-bases/{id}/documents` | List documents |
| DELETE | `/knowledge-bases/{id}/documents/{doc_id}` | Soft delete document |
| POST | `/knowledge-bases/{id}/query` | Query knowledge base (RAG search) |

### 5.12 Notifications

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/notifications` | List notifications |
| POST | `/notifications` | Schedule notification |
| GET | `/notifications/{id}` | Get notification status |
| POST | `/notifications/{id}/cancel` | Cancel pending notification |

### 5.13 Analytics & Logs

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/analytics/calls` | Call volume, duration, outcomes |
| GET | `/analytics/bookings` | Booking conversion rate, popular slots |
| GET | `/analytics/sentiment` | Sentiment distribution over time |
| GET | `/job-logs` | Background job execution logs |
| GET | `/audit-logs` | Audit trail (admin only) |

### 5.14 Webhooks (Internal)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/webhooks/livekit` | LiveKit room events (room_created, participant_joined, etc.) |
| POST | `/webhooks/google-calendar` | Google Calendar push notifications |

---

## 6. Conversation Finite State Machine (FSM)

The agent's conversation is controlled by an explicit FSM. The LLM is the language layer; the FSM is the control plane.

### States

| State | Description |
|-------|-------------|
| `idle` | Call connected, waiting to start |
| `greeting` | Agent introduces itself, verifies identity |
| `purpose` | Clarify booking reason, handle "just checking" |
| `schedule_query` | Read available slots, present options |
| `slot_select` | User picks slot → Redis hold → confirmation prompt |
| `confirmation` | Handle yes/no/let me think/what about tomorrow? |
| `booking` | Execute 3-layer defense, write to DB + Calendar |
| `reschedule` | Release old, find new, re-book |
| `closing` | Confirm details, end call |
| `interrupted` | Barge-in detected, pause TTS, re-evaluate intent |
| `escalation` | Transfer to human, log, notify admin |

### State Transitions

```
greeting → purpose → schedule_query → slot_select → confirmation → booking → closing
                    ↑                    ↓                ↓
                    └──────────────────┘   (user asks schedule mid-booking: push stack)

Any state ──barge-in──→ interrupted ──resume──→ [pop stack]
                                    ──new intent──→ [route to new state]

confirmation ──yes──→ booking
confirmation ──different slot──→ slot_select
confirmation ──check schedule──→ schedule_query (push current state to fsm_stack)
confirmation ──reschedule──→ reschedule

booking ──success──→ closing
booking ──calendar fail──→ confirmation ("Let me try again")
```

### Stack-Based Context Switching

When a user interrupts mid-booking to ask "what's your schedule?":
1. Push current state + intent context to `fsm_stack`
2. Transition to `schedule_query`
3. After answering, pop stack and resume: "Now, back to your booking — shall I confirm 3pm?"

---

## 7. Three-Layer Booking Defense

This prevents double-booking when multiple users want the same slot simultaneously.

### Layer 1: Redis Tentative Hold

```python
# When user selects a slot
result = await redis.set(
    f"slot:{tenant_id}:{slot_id}",
    session_id,
    nx=True,      # Only if not exists
    ex=300        # Expires in 5 minutes (call drop protection)
)
if result is None:
    # Slot is already held by another call
    agent.say("That slot was just taken. Next available is 4pm.")
```

### Layer 2: Database Pessimistic Lock

```python
async with db.transaction():
    slot = await db.fetchrow("""
        SELECT * FROM slots 
        WHERE id = $1 AND tenant_id = $2 
        FOR UPDATE
    """, slot_id, tenant_id)
    
    if slot["status"] != "available" and slot["held_by_session"] != session_id:
        raise SlotTakenError()
    
    await db.execute("""
        UPDATE slots SET status = 'held', held_by_session = $1, held_by_customer = $2, held_until = $3
        WHERE id = $4
    """, session_id, customer_id, now() + timedelta(minutes=5), slot_id)
```

### Layer 3: Google Calendar ETag Optimistic Concurrency

```python
# 1. Re-verify with freebusy
freebusy = await google_cal.freebusy_query(tenant_id, slot_start, slot_end)
if not freebusy.is_free:
    raise SlotTakenError()

# 2. Insert with ETag check
event = await google_cal.insert_event(
    tenant_id=tenant_id,
    event_body=event_body,
    if_match=etag_from_previous_read  # 412 Precondition Failed if changed
)

# 3. Atomic DB update
await db.execute("""
    UPDATE slots SET status = 'booked', version = version + 1 WHERE id = $1
""", slot_id)

await db.appointments.insert(
    tenant_id=tenant_id,
    customer_id=customer_id,
    slot_id=slot_id,
    google_event_id=event["id"],
    sync_status="synced"
)
```

### Failure Handling

| Layer Fails | Action |
|-------------|--------|
| L1 (Redis NX) | Agent says "Slot taken, here are alternatives" |
| L2 (DB lock) | Agent says "Let me check again", re-queries freebusy |
| L3 (Calendar 412) | Agent says "That slot just got booked. Alternatives?" Release DB lock, clear Redis |

### Cleanup

APScheduler job every 60 seconds:
```sql
UPDATE slots 
SET status = 'available', held_by_session = NULL, held_by_customer = NULL, held_until = NULL
WHERE held_until < NOW() AND status = 'held';
```

Redis TTL handles most cases; DB cleanup is the safety net.

---

## 8. Data Flows

### Outbound Call Flow

```
Client fills form → POST /customers (or /leads)
    ↓
POST /calls/outbound
    ↓
FastAPI:
  1. Create customer record
  2. Create call_session (status: ringing)
  3. Create LiveKit room
  4. BackgroundTask: Send SMS with room link
  5. BackgroundTask: Spawn Pipecat agent worker (HTTP POST to worker pool)
    ↓
User clicks SMS link → Browser joins LiveKit room
    ↓
Agent worker joins room → FSM starts at GREETING
    ↓
Conversation → Booking Defense → Google Calendar
    ↓
Call ends → POST /calls/{id}/end
```

### Inbound Call Flow

```
User clicks "Call Us" on website
    ↓
Browser joins LiveKit room
    ↓
LiveKit webhook → POST /webhooks/livekit (room_created)
    ↓
POST /calls/inbound
    ↓
FastAPI:
  1. Create call_session (status: active)
  2. Ask for name/phone → lookup customer
  3. Spawn Pipecat agent worker
    ↓
Agent joins room → FSM starts at GREETING
    ↓
Same conversation + booking flow as outbound
```

### Race Condition Flow

```
Caller A wants 3pm          Caller B wants 3pm (simultaneous)
    ↓                           ↓
Redis SET NX → SUCCESS        Redis SET NX → FAIL (key exists)
    ↓                           ↓
DB SELECT FOR UPDATE          Agent: "Sorry, that slot was just taken."
    ↓
Google Calendar insert
    ↓
DB UPDATE slot → booked
    ↓
Redis DEL slot key
```

---

## 9. Corner Cases & Solutions

| # | Scenario | Solution |
|---|----------|----------|
| 1 | Two users want same slot | 3-layer defense: Redis NX → DB FOR UPDATE → Calendar ETag |
| 2 | Call drops during booking | Redis TTL (5 min) auto-releases. DB tx rolls back on disconnect. Cleanup job marks stale holds. |
| 3 | User asks schedule mid-booking | FSM push state to stack → SCHEDULE_QUERY → pop stack → resume booking |
| 4 | User interrupts confirmation | VAD barge-in → stop TTS → INTERRUPTED state → NLU re-evaluate → route |
| 5 | Calendar API down during booking | Retry 3x. If fail: DB status="pending_sync", extend hold, APScheduler retries every 2 min. Agent: "Confirmed, syncing shortly." |
| 6 | Many inbound calls at once | LiveKit SFU handles media. Worker pool auto-scales. Redis connection pool sized for peak. |
| 7 | Tenant isolation breach | RLS on every query. API middleware validates tenant_id. Redis keys namespaced. |
| 8 | Book then immediately reschedule | RESCHEDULE state → Delete calendar event (ETag) → release slot → new booking flow |
| 9 | Ambiguous time (AM/PM) | NLU entity extraction → clarification loop → FSM stays in SLOT_SELECT until unambiguous |
| 10 | User wants human | ESCALATION state → log transcript → webhook/SMS admin → offer callback → end gracefully |

---

## 10. Celery-Free Background Jobs (APScheduler)

Run APScheduler in a **separate single-instance container** to avoid duplicate execution.

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

@scheduler.scheduled_job('interval', seconds=60, id='sync_cal')
async def sync_calendar():
    for tenant in await get_all_tenants():
        if not tenant.calendar_sync_enabled:
            continue
        try:
            events = await google_cal.sync(tenant.id)
            await db.slots.reconcile(tenant.id, events)
            await db.calendar_integrations.update_last_sync(tenant.id)
            await log_job('calendar_sync', 'completed', tenant_id=tenant.id)
        except Exception as e:
            await log_job('calendar_sync', 'failed', tenant_id=tenant.id, error=str(e))

@scheduler.scheduled_job('interval', seconds=60, id='cleanup_holds')
async def cleanup_stale_holds():
    await db.execute("""
        UPDATE slots 
        SET status = 'available', held_by_session = NULL, held_by_customer = NULL
        WHERE held_until < NOW() AND status = 'held'
    """)
    # Also clean Redis pattern
    await redis.delete_pattern('slot:*:held')

@scheduler.scheduled_job('interval', seconds=120, id='retry_bookings')
async def retry_pending_bookings():
    pending = await db.appointments.get_pending_sync()
    for appt in pending:
        try:
            event = await google_cal.insert_event(appt.tenant_id, appt.to_event_body())
            await db.appointments.mark_synced(appt.id, event['id'])
        except Exception as e:
            await db.appointments.increment_retry(appt.id, str(e))

@scheduler.scheduled_job('interval', minutes=15, id='send_reminders')
async def send_reminders():
    upcoming = await db.appointments.get_upcoming(minutes=30)
    for appt in upcoming:
        await send_sms_reminder(appt.customer_phone, appt.start_time)
        await db.notifications.mark_sent(appt.id)

scheduler.start()
```

### FastAPI BackgroundTasks

```python
from fastapi import BackgroundTasks

@app.post('/calls/outbound')
async def create_outbound_call(data: OutboundCallRequest, bg: BackgroundTasks):
    customer = await db.customers.create(data.customer)
    session = await db.call_sessions.create(tenant_id, customer.id, 'outbound')
    room = await livekit.create_room()
    
    # Fire-and-forget
    bg.add_task(send_sms, customer.phone, room.url)
    bg.add_task(spawn_agent_worker, room.name, session.id, tenant_id)
    
    return {'session_id': session.id, 'room_url': room.url}
```



---

## 12. Performance Targets

| Metric | Target |
|--------|--------|
| End-to-end latency | < 500ms (perceived) |
| STT latency | < 200ms |
| LLM time-to-first-token | < 300ms |
| TTS latency | < 100ms (Kokoro) |
| Concurrent calls per worker | 10-20 per CPU core |
| Total concurrent capacity | 100+ per worker node |
| Cost | $0 (self-hosted stack) |

---

## 13. File Structure (Recommended)

```
voice-agent-saas/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app factory
│   │   ├── config.py            # Settings, env vars
│   │   ├── dependencies.py      # DB, Redis, Auth deps
│   │   ├── middleware/
│   │   │   ├── tenant.py        # tenant_id extraction
│   │   │   ├── auth.py          # JWT validation
│   │   │   └── audit.py         # Audit logging middleware
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   ├── auth.py
│   │   │   │   ├── tenants.py
│   │   │   │   ├── users.py
│   │   │   │   ├── customers.py
│   │   │   │   ├── leads.py
│   │   │   │   ├── services.py
│   │   │   │   ├── slots.py
│   │   │   │   ├── appointments.py
│   │   │   │   ├── calls.py
│   │   │   │   ├── calendar.py
│   │   │   │   ├── ai_config.py
│   │   │   │   ├── voice_config.py
│   │   │   │   ├── knowledge.py
│   │   │   │   ├── notifications.py
│   │   │   │   ├── analytics.py
│   │   │   │   └── webhooks.py
│   │   ├── core/
│   │   │   ├── security.py      # Password hashing, JWT
│   │   │   └── exceptions.py    # Custom exceptions
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── tenant.py
│   │   │   ├── user.py
│   │   │   ├── customer.py
│   │   │   ├── lead.py
│   │   │   ├── service.py
│   │   │   ├── slot.py
│   │   │   ├── appointment.py
│   │   │   ├── call_session.py
│   │   │   ├── calendar.py
│   │   │   ├── ai_config.py
│   │   │   ├── voice_config.py
│   │   │   ├── knowledge.py
│   │   │   ├── notification.py
│   │   │   ├── job_log.py
│   │   │   ├── integration.py
│   │   │   └── audit_log.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── tenant_service.py
│   │   │   ├── customer_service.py
│   │   │   ├── slot_service.py
│   │   │   ├── booking_service.py      # 3-layer defense
│   │   │   ├── calendar_service.py
│   │   │   ├── call_service.py
│   │   │   ├── notification_service.py
│   │   │   └── knowledge_service.py
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # SQLAlchemy base
│   │   │   ├── session.py           # Async session manager
│   │   │   └── migrations/          # Alembic
│   │   ├── scheduler/
│   │   │   ├── __init__.py
│   │   │   └── jobs.py              # APScheduler jobs
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   ├── pipeline.py          # Pipecat pipeline builder
│   │   │   ├── fsm.py               # Finite state machine
│   │   │   ├── tools.py             # LLM tool definitions
│   │   │   └── worker.py            # Agent worker process
│   │   └── utils/
│   │       ├── redis_client.py
│   │       ├── google_cal.py
│   │       ├── livekit_client.py
│   │       └── sms_gateway.py
│   ├── tests/
│   ├── alembic.ini
│   ├── requirements.txt
│   ├── Dockerfile
│   └── docker-compose.yml
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── hooks/
│   │   ├── services/
│   │   └── stores/
│   ├── package.json
│   └── Dockerfile
├── scheduler-service/           # Separate APScheduler container
│   ├── Dockerfile
│   └── main.py
├── docker-compose.yml           # Full stack: API + DB + Redis + LiveKit + Scheduler
└── README.md
```

---

## 14. Key Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Voice framework | Pipecat + LiveKit | Full pipeline control for complex FSM + multi-tenancy |
| No Celery | BackgroundTasks + APScheduler | Fewer moving parts. Voice workload doesn't need distributed queue. |
| Database pattern | Shared schema + RLS | Standard B2B SaaS. Simplest to maintain. |
| Soft delete | `deleted_at` on every table | Enterprise requirement. GDPR compliance. Never hard-delete business data. |
| Slot table | Separate from appointments | Enables 3-layer concurrency defense. Slots are potential; appointments are confirmed. |
| Transcript | Structured JSONB events | Replayable, searchable, analytics-ready. Not a raw text blob. |
| AI config | `provider_config` JSONB | Switch LLM providers without migration. |
| Audit logs | Append-only table | Compliance. Security. Enterprise sales requirement. |
| Redis role | Cache + state only | Not a Celery broker. If Redis dies, DB is source of truth. |

---

*Generated for Araaf (ArafathUIU) — Production-Grade Voice Agent SaaS — 2026*
"""

with open('/mnt/agents/output/voice_agent_project_context.md', 'w') as f:
    f.write(content)

print("Saved! File size:", len(content), "characters")
