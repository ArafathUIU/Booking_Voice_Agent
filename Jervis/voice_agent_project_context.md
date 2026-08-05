# Production Multi-Tenant AI Voice Agent SaaS
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
| **Task Tracking** | **PostgreSQL job_logs table** | Replaces Celery task result backend. | Free |
| **Frontend** | **React** | Web call interface, form submission, admin dashboard. | Free |

### Why Pipecat over LiveKit Agents?
LiveKit Agents bundles transport + agent logic -- great for simple demos but opinionated. Pipecat gives pure pipeline control you drop on top of LiveKit's transport. You get LiveKit's media plumbing + Pipecat's flexible orchestration. For multi-tenant booking with a 10-state FSM, Pipecat is the safer production path.

### Why Celery-Free?
Celery adds 3 moving parts (broker, workers, beat) that are not needed:
- Fire-and-forget tasks (SMS, spawn call) -> FastAPI BackgroundTasks
- Periodic tasks (sync, cleanup, reminders) -> APScheduler
- The heavy work (STT/LLM/TTS) happens inside stateful Pipecat workers, not background jobs

---

## 3. Multi-Tenant Architecture

### Pattern: Shared Schema with tenant_id
Every table has a tenant_id column. Row-Level Security (RLS) policies enforce filtering.

### Tenant Context Propagation
```
API Gateway extracts tenant from JWT or subdomain
    |
Sets app.current_tenant_id on every DB connection (transaction-scoped)
    |
All queries automatically filtered by RLS
    |
Redis keys namespaced: slot:{tenant_id}:{slot_id}
```

### RLS Policy Template
```sql
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::UUID);
```

Apply to: users, customers, leads, services, slots, appointments, business_hours, calendar_integrations, ai_configs, voice_configs, knowledge_bases, knowledge_documents, call_sessions, notifications, job_logs, integrations.

audit_logs: Optionally skip RLS for cross-tenant admin views, or apply with superuser bypass.

---

## 4. Database Schema (DBML)

```dbml
Table tenants {
  id uuid [pk, default: `gen_random_uuid()`]
  name varchar
  slug varchar [unique]
  email varchar
  phone varchar
  timezone varchar [default: 'UTC']
  status varchar [default: 'active']
  agent_name varchar [default: 'AI Assistant']
  agent_role varchar [default: 'Booking Assistant']
  default_calendar_id uuid
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
}

Table tenant_settings {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  slot_duration_minutes integer [default: 30]
  advance_booking_days integer [default: 30]
  min_notice_hours integer [default: 2]
  buffer_between_appointments_minutes integer [default: 0]
  greeting_script text
  escalation_enabled boolean [default: true]
  max_hold_minutes integer [default: 5]
  deleted_at timestamp [null]
  updated_at timestamp [default: `now()`]
}

Table users {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  name varchar
  email varchar
  password_hash varchar
  role varchar
  is_active boolean [default: true]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes { (tenant_id, email) [unique, name: 'idx_users_tenant_email'] }
}

Table customers {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  full_name varchar
  email varchar
  phone varchar
  address text
  notes text
  preferred_language varchar [default: 'en']
  preferred_voice_language varchar [default: 'en-US']
  preferred_service_id uuid [null]
  last_call_at timestamp [null]
  last_booking_at timestamp [null]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, phone) [name: 'idx_customers_tenant_phone']
    (tenant_id, email) [name: 'idx_customers_tenant_email']
  }
}

Table leads {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  customer_id uuid [null]
  source varchar
  purpose text
  status varchar [default: 'new']
  priority varchar [default: 'medium']
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes { (tenant_id, status, priority) [name: 'idx_leads_tenant_status_priority'] }
}

Table services {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  name varchar
  description text
  duration_minutes integer
  price decimal
  is_active boolean [default: true]
  buffer_minutes integer [default: 0]
  max_bookings_per_slot integer [default: 1]
  color varchar [default: '#3b82f6']
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  Indexes { (tenant_id, is_active) [name: 'idx_services_tenant_active'] }
}

Table slots {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  service_id uuid [not null]
  start_time timestamp [not null]
  end_time timestamp [not null]
  status varchar [default: 'available']
  held_by_session uuid [null]
  held_by_customer uuid [null]
  held_until timestamp [null]
  version integer [default: 1]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, start_time, status) [name: 'idx_slots_tenant_time_status']
    (held_until) [name: 'idx_slots_held_until']
    (held_by_customer) [name: 'idx_slots_held_by_customer']
  }
  Note: 'Denormalized held_by_customer avoids JOIN during cleanup if session is purged.'
}

Table appointments {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  customer_id uuid [not null]
  service_id uuid [not null]
  session_id uuid [null]
  slot_id uuid [not null]
  calendar_integration_id uuid [not null]
  title varchar
  start_time timestamp [not null]
  end_time timestamp [not null]
  timezone varchar
  status varchar [default: 'confirmed']
  google_event_id varchar
  notes text
  held_at timestamp [null]
  confirmed_at timestamp [null]
  version integer [default: 1]
  sync_status varchar [default: 'synced']
  sync_error text [null]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, start_time, status) [name: 'idx_appointments_tenant_time_status']
    (tenant_id, google_event_id) [name: 'idx_appointments_tenant_google']
    (slot_id) [name: 'idx_appointments_slot_id']
    (sync_status) [name: 'idx_appointments_sync_status']
  }
}

Table business_hours {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  weekday integer
  opens_at time
  closes_at time
  is_closed boolean [default: false]
  deleted_at timestamp [null]
  Indexes { (tenant_id, weekday) [name: 'idx_business_hours_tenant_weekday'] }
}

Table calendar_integrations {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  provider varchar [default: 'google']
  calendar_id varchar
  access_token text
  refresh_token text
  expires_at timestamp
  sync_enabled boolean [default: true]
  last_sync_at timestamp [null]
  webhook_channel_id varchar [null]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, provider) [name: 'idx_calendar_tenant_provider']
    (webhook_channel_id) [name: 'idx_calendar_webhook_channel']
  }
}

Table ai_configs {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  system_prompt text
  llm_provider varchar
  llm_model varchar
  stt_provider varchar
  tts_provider varchar
  temperature decimal [default: 0.7]
  max_tokens integer [default: 150]
  provider_config jsonb [default: '{}']
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes { (tenant_id) [unique, name: 'idx_ai_configs_tenant_unique'] }
  Note: 'provider_config stores provider-specific keys (top_p, response_format, etc.) without schema changes on provider switch.'
}

Table voice_configs {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  provider varchar
  voice_id varchar
  language varchar [default: 'en-US']
  stability decimal [default: 0.5]
  similarity decimal [default: 0.75]
  speaking_rate decimal [default: 1.0]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  Indexes { (tenant_id) [unique, name: 'idx_voice_configs_tenant_unique'] }
}

Table knowledge_bases {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  name varchar
  description text
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
}

Table knowledge_documents {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  kb_id uuid [not null]
  filename varchar
  file_path text
  file_type varchar
  file_size bigint
  status varchar [default: 'processing']
  document_hash varchar [null]
  chunking_strategy varchar [default: 'recursive_512']
  embedding_version varchar [null]
  indexed_at timestamp [null]
  deleted_at timestamp [null]
  uploaded_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, status) [name: 'idx_kdocs_tenant_status']
    (document_hash) [name: 'idx_kdocs_hash']
  }
}

Table call_sessions {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  customer_id uuid [null]
  lead_id uuid [null]
  service_id uuid [null]
  session_type varchar
  livekit_room varchar [unique]
  status varchar [default: 'ringing']
  ended_reason varchar [null]
  fsm_state varchar [default: 'idle']
  fsm_stack jsonb [default: '[]']
  intent_context jsonb [default: '{}']
  booking_outcome varchar [null]
  agent_config_snapshot jsonb [default: '{}']
  started_at timestamp
  ended_at timestamp
  duration_seconds integer
  transcript jsonb [default: '[]']
  summary text
  sentiment varchar
  detected_intent varchar
  total_input_tokens integer
  total_output_tokens integer
  total_cost decimal
  metadata jsonb [default: '{}']
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  updated_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, status) [name: 'idx_sessions_tenant_status']
    (livekit_room) [name: 'idx_sessions_livekit_room']
    (customer_id) [name: 'idx_sessions_customer_id']
    (ended_reason) [name: 'idx_sessions_ended_reason']
  }
  Note: 'Transcript event schema per element: { id: uuid, type: "user_speech|agent_speech|tool_call|tool_result|interruption|state_change|error", speaker: "user|agent", text: string, tool: {name, arguments}, tool_result: {status, data}, latency_ms: {stt, llm, tts}, timestamp: ISO8601 }'
}

Table notifications {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  customer_id uuid [not null]
  type varchar
  message text
  status varchar [default: 'pending']
  scheduled_at timestamp
  sent_at timestamp
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  Indexes { (tenant_id, status, scheduled_at) [name: 'idx_notifications_tenant_status_scheduled'] }
}

Table job_logs {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [null]
  job_type varchar
  job_id varchar
  status varchar [default: 'started']
  payload jsonb [default: '{}']
  result jsonb [default: '{}']
  error_message text [null]
  started_at timestamp
  completed_at timestamp [null]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
  Indexes {
    (job_type, status) [name: 'idx_job_logs_type_status']
    (started_at) [name: 'idx_job_logs_started_at']
  }
}

Table integrations {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  provider varchar
  config json
  is_active boolean [default: true]
  deleted_at timestamp [null]
  created_at timestamp [default: `now()`]
}

Table audit_logs {
  id uuid [pk, default: `gen_random_uuid()`]
  tenant_id uuid [not null]
  actor_type varchar [default: 'user']
  actor_id uuid [null]
  action varchar
  entity_type varchar
  entity_id uuid [null]
  old_values jsonb [null]
  new_values jsonb [null]
  metadata jsonb [default: '{}']
  created_at timestamp [default: `now()`]
  Indexes {
    (tenant_id, entity_type, entity_id) [name: 'idx_audit_tenant_entity']
    (actor_id, created_at) [name: 'idx_audit_actor_created']
    (created_at) [name: 'idx_audit_created_at']
  }
  Note: 'Append-only. Never updated or deleted. Implement via PostgreSQL triggers or service-layer middleware.'
}

// Relationships
Ref: tenant_settings.tenant_id > tenants.id
Ref: users.tenant_id > tenants.id
Ref: customers.tenant_id > tenants.id
Ref: customers.preferred_service_id > services.id
Ref: leads.tenant_id > tenants.id
Ref: leads.customer_id > customers.id
Ref: services.tenant_id > tenants.id
Ref: slots.tenant_id > tenants.id
Ref: slots.service_id > services.id
Ref: slots.held_by_session > call_sessions.id
Ref: slots.held_by_customer > customers.id
Ref: appointments.tenant_id > tenants.id
Ref: appointments.customer_id > customers.id
Ref: appointments.service_id > services.id
Ref: appointments.session_id > call_sessions.id
Ref: appointments.slot_id > slots.id
Ref: appointments.calendar_integration_id > calendar_integrations.id
Ref: business_hours.tenant_id > tenants.id
Ref: calendar_integrations.tenant_id > tenants.id
Ref: ai_configs.tenant_id > tenants.id
Ref: voice_configs.tenant_id > tenants.id
Ref: knowledge_bases.tenant_id > tenants.id
Ref: knowledge_documents.tenant_id > tenants.id
Ref: knowledge_documents.kb_id > knowledge_bases.id
Ref: call_sessions.tenant_id > tenants.id
Ref: call_sessions.customer_id > customers.id
Ref: call_sessions.lead_id > leads.id
Ref: call_sessions.service_id > services.id
Ref: notifications.tenant_id > tenants.id
Ref: notifications.customer_id > customers.id
Ref: job_logs.tenant_id > tenants.id
Ref: integrations.tenant_id > tenants.id
Ref: audit_logs.tenant_id > tenants.id
Ref: tenants.default_calendar_id > calendar_integrations.id
```

---

## 5. API Endpoints

### 5.1 Authentication & Tenant Management

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | /auth/register | Register a new tenant + admin user |
| POST | /auth/login | Login, returns JWT with tenant_id |
| POST | /auth/refresh | Refresh access token |
| POST | /auth/logout | Invalidate token |
| GET | /auth/me | Current user profile |
| GET | /tenants/me | Get current tenant details |
| PUT | /tenants/me | Update tenant profile |
| PUT | /tenants/me/settings | Update tenant_settings |

### 5.2 Users (Admin Dashboard)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /users | List users in tenant |
| POST | /users | Create user (admin only) |
| GET | /users/{id} | Get user details |
| PUT | /users/{id} | Update user |
| DELETE | /users/{id} | Soft delete user |

### 5.3 Customers

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /customers | List customers (pagination, search) |
| POST | /customers | Create customer |
| GET | /customers/{id} | Get customer details |
| PUT | /customers/{id} | Update customer |
| DELETE | /customers/{id} | Soft delete customer |
| GET | /customers/{id}/history | Call + booking history |
| GET | /customers/lookup | Lookup by phone or email (inbound calls) |

### 5.4 Leads

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /leads | List leads |
| POST | /leads | Create lead (from web form) |
| GET | /leads/{id} | Get lead details |
| PUT | /leads/{id} | Update lead status/priority |
| POST | /leads/{id}/convert | Convert lead to customer + appointment |
| DELETE | /leads/{id} | Soft delete lead |

### 5.5 Services

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /services | List active services |
| POST | /services | Create service |
| GET | /services/{id} | Get service details |
| PUT | /services/{id} | Update service |
| DELETE | /services/{id} | Soft delete service |

### 5.6 Slots & Availability

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /availability | Get available slots for date range. Query: start_date, end_date, service_id |
| POST | /slots/hold | Tentatively hold a slot. Returns hold_token. Body: slot_id, customer_id, session_id |
| POST | /slots/release | Release a held slot. Body: slot_id, hold_token |
| GET | /slots/{id} | Get slot details |

### 5.7 Appointments

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /appointments | List appointments (filters: date range, customer, status) |
| POST | /appointments | Atomic booking endpoint. Body: slot_id, customer_id, service_id, session_id, notes |
| GET | /appointments/{id} | Get appointment details |
| PUT | /appointments/{id} | Update appointment (notes, status) |
| DELETE | /appointments/{id} | Cancel (soft delete + delete from Google Calendar) |
| POST | /appointments/{id}/reschedule | Reschedule to new slot. Atomic: release old, hold new, book new |
| GET | /appointments/{id}/sync-status | Check Google Calendar sync status |

### 5.8 Call Sessions

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /calls | List call sessions |
| POST | /calls/outbound | Trigger outbound call. Creates customer, LiveKit room, spawns agent via BackgroundTask |
| POST | /calls/inbound | Handle inbound call. Webhook from LiveKit. Lookup customer, create session, spawn agent |
| GET | /calls/{id} | Get call session details |
| GET | /calls/{id}/transcript | Get structured transcript |
| POST | /calls/{id}/end | End call. Updates status, duration, ended_reason |
| POST | /calls/{id}/escalate | Mark as escalated to human |
| GET | /calls/{id}/recording | Get recording URL |

### 5.9 Calendar Integration

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /calendar/auth | Get Google OAuth URL |
| POST | /calendar/callback | OAuth callback. Stores tokens |
| GET | /calendar/status | Check connection status |
| POST | /calendar/sync | Trigger manual sync (BackgroundTask) |
| GET | /calendar/events | List calendar events (proxy to Google) |
| DELETE | /calendar | Disconnect calendar (soft delete) |
| POST | /calendar/webhook | Google Calendar push notification webhook |

### 5.10 AI & Voice Configuration

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /ai-config | Get AI config |
| PUT | /ai-config | Update AI config (system_prompt, model, temperature, provider_config) |
| GET | /voice-config | Get voice config |
| PUT | /voice-config | Update voice config |
| POST | /ai-config/test | Test AI config with sample prompt |

### 5.11 Knowledge Base

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /knowledge-bases | List KBs |
| POST | /knowledge-bases | Create KB |
| GET | /knowledge-bases/{id} | Get KB details |
| DELETE | /knowledge-bases/{id} | Soft delete KB |
| POST | /knowledge-bases/{id}/documents | Upload document |
| GET | /knowledge-bases/{id}/documents | List documents |
| DELETE | /knowledge-bases/{id}/documents/{doc_id} | Soft delete document |
| POST | /knowledge-bases/{id}/query | RAG search query |

### 5.12 Notifications

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /notifications | List notifications |
| POST | /notifications | Schedule notification |
| GET | /notifications/{id} | Get status |
| POST | /notifications/{id}/cancel | Cancel pending |

### 5.13 Analytics & Logs

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | /analytics/calls | Call volume, duration, outcomes |
| GET | /analytics/bookings | Conversion rate, popular slots |
| GET | /analytics/sentiment | Sentiment distribution |
| GET | /job-logs | Background job logs |
| GET | /audit-logs | Audit trail (admin only) |

### 5.14 Webhooks (Internal)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | /webhooks/livekit | LiveKit room events |
| POST | /webhooks/google-calendar | Google Calendar push notifications |

---

## 6. Conversation Finite State Machine (FSM)

### States

| State | Description |
|-------|-------------|
| idle | Call connected, waiting |
| greeting | Agent introduces, verifies identity |
| purpose | Clarify booking reason |
| schedule_query | Read available slots |
| slot_select | User picks slot -> Redis hold -> confirmation prompt |
| confirmation | Handle yes/no/let me think |
| booking | Execute 3-layer defense |
| reschedule | Release old, find new |
| closing | Confirm details, end |
| interrupted | Barge-in detected, re-evaluate |
| escalation | Transfer to human |

### State Transitions

```
greeting -> purpose -> schedule_query -> slot_select -> confirmation -> booking -> closing
                     ^                    v                v
                     |____________________|   (user asks schedule: push stack)

Any state --barge-in--> interrupted --resume--> [pop stack]
                                     --new intent--> [route to new state]

confirmation --yes--> booking
confirmation --different slot--> slot_select
confirmation --check schedule--> schedule_query (push to stack)
confirmation --reschedule--> reschedule

booking --success--> closing
booking --calendar fail--> confirmation ("Let me try again")
```

### Stack-Based Context Switching
When user interrupts mid-booking to ask "what's your schedule?":
1. Push current state + intent_context to fsm_stack
2. Transition to schedule_query
3. After answering, pop stack and resume: "Now, back to your booking -- shall I confirm 3pm?"

---

## 7. Three-Layer Booking Defense

### Layer 1: Redis Tentative Hold
```python
result = await redis.set(
    f"slot:{tenant_id}:{slot_id}",
    session_id,
    nx=True,      # Only if not exists
    ex=300        # 5 min TTL (call drop protection)
)
if result is None:
    agent.say("That slot was just taken. Next available is 4pm.")
```

### Layer 2: Database Pessimistic Lock
```python
async with db.transaction():
    slot = await db.fetchrow(
        "SELECT * FROM slots WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
        slot_id, tenant_id
    )
    if slot["status"] != "available" and slot["held_by_session"] != session_id:
        raise SlotTakenError()

    await db.execute(
        "UPDATE slots SET status='held', held_by_session=$1, held_by_customer=$2, held_until=$3 WHERE id=$4",
        session_id, customer_id, now() + timedelta(minutes=5), slot_id
    )
```

### Layer 3: Google Calendar ETag
```python
freebusy = await google_cal.freebusy_query(tenant_id, slot_start, slot_end)
if not freebusy.is_free:
    raise SlotTakenError()

event = await google_cal.insert_event(
    tenant_id=tenant_id,
    event_body=event_body,
    if_match=etag_from_previous_read  # 412 if changed
)

await db.execute("UPDATE slots SET status='booked', version=version+1 WHERE id=$1", slot_id)
await db.appointments.insert(tenant_id, customer_id, slot_id, event["id"], sync_status="synced")
```

### Failure Handling
| Layer Fails | Action |
|-------------|--------|
| L1 (Redis NX) | "Slot taken, here are alternatives" |
| L2 (DB lock) | "Let me check again", re-query freebusy |
| L3 (Calendar 412) | "That slot just got booked. Alternatives?" Release DB lock, clear Redis |

### Cleanup (APScheduler)
```sql
UPDATE slots SET status='available', held_by_session=NULL, held_by_customer=NULL
WHERE held_until < NOW() AND status='held';
```

---

## 8. Data Flows

### Outbound Call
```
Form -> POST /customers -> POST /calls/outbound -> FastAPI:
  1. Create customer
  2. Create call_session (ringing)
  3. Create LiveKit room
  4. BackgroundTask: Send SMS with room link
  5. BackgroundTask: Spawn Pipecat agent worker
-> User clicks SMS -> Browser joins room
-> Agent joins -> FSM greeting -> Conversation -> Booking Defense -> Google Calendar
-> Call ends -> POST /calls/{id}/end
```

### Inbound Call
```
User clicks "Call Us" -> Browser joins LiveKit room
-> LiveKit webhook -> POST /webhooks/livekit (room_created)
-> POST /calls/inbound -> FastAPI:
  1. Create call_session (active)
  2. Ask for name/phone -> lookup customer
  3. Spawn Pipecat agent worker
-> Agent joins -> FSM greeting -> Same booking flow
```

### Race Condition
```
Caller A wants 3pm          Caller B wants 3pm
    |                           |
Redis SET NX -> SUCCESS       Redis SET NX -> FAIL
    |                           |
DB SELECT FOR UPDATE          Agent: "Sorry, taken."
    |
Google Calendar insert
    |
DB UPDATE -> booked
    |
Redis DEL key
```

---

## 9. Corner Cases & Solutions

| # | Scenario | Solution |
|---|----------|----------|
| 1 | Two users want same slot | 3-layer defense: Redis NX -> DB FOR UPDATE -> Calendar ETag |
| 2 | Call drops during booking | Redis TTL auto-releases. DB tx rolls back. Cleanup job handles stale. |
| 3 | User asks schedule mid-booking | FSM push stack -> schedule_query -> pop stack -> resume |
| 4 | User interrupts confirmation | VAD -> stop TTS -> interrupted state -> NLU re-evaluate |
| 5 | Calendar API down | Retry 3x. DB status=pending_sync. APScheduler retries every 2 min. |
| 6 | Many inbound calls | LiveKit SFU handles media. Worker pool auto-scales. |
| 7 | Tenant isolation breach | RLS every query. Middleware validates tenant_id. Redis namespaced keys. |
| 8 | Book then reschedule | RESCHEDULE state -> Delete calendar event (ETag) -> release -> new booking |
| 9 | Ambiguous time (AM/PM) | NLU clarification loop. FSM stays in slot_select until unambiguous. |
| 10 | User wants human | ESCALATION -> log transcript -> webhook/SMS admin -> callback -> end |

---

## 10. Celery-Free Background Jobs

Run APScheduler in a **separate single-instance container**.

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
scheduler = AsyncIOScheduler()

@scheduler.scheduled_job('interval', seconds=60, id='sync_cal')
async def sync_calendar():
    for tenant in await get_all_tenants():
        if not tenant.calendar_sync_enabled: continue
        try:
            events = await google_cal.sync(tenant.id)
            await db.slots.reconcile(tenant.id, events)
            await log_job('calendar_sync', 'completed', tenant_id=tenant.id)
        except Exception as e:
            await log_job('calendar_sync', 'failed', tenant_id=tenant.id, error=str(e))

@scheduler.scheduled_job('interval', seconds=60, id='cleanup_holds')
async def cleanup_stale_holds():
    await db.execute("UPDATE slots SET status='available', held_by_session=NULL, held_by_customer=NULL WHERE held_until < NOW() AND status='held'")
    await redis.delete_pattern('slot:*:held')

@scheduler.scheduled_job('interval', seconds=120, id='retry_bookings')
async def retry_pending_bookings():
    for appt in await db.appointments.get_pending_sync():
        try:
            event = await google_cal.insert_event(appt.tenant_id, appt.to_event_body())
            await db.appointments.mark_synced(appt.id, event['id'])
        except Exception as e:
            await db.appointments.increment_retry(appt.id, str(e))

@scheduler.scheduled_job('interval', minutes=15, id='send_reminders')
async def send_reminders():
    for appt in await db.appointments.get_upcoming(minutes=30):
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
    bg.add_task(send_sms, customer.phone, room.url)
    bg.add_task(spawn_agent_worker, room.name, session.id, tenant_id)
    return {'session_id': session.id, 'room_url': room.url}
```

---

## 11. Implementation Roadmap

### Phase 1: Foundation (Week 1-2)
- FastAPI project structure
- PostgreSQL schema + Alembic migrations
- RLS policies on all tenant tables
- JWT auth + tenant context middleware
- Basic CRUD endpoints (tenants, users, customers, services)
- Soft delete middleware

### Phase 2: Voice Pipeline (Week 3-4)
- LiveKit server (Docker)
- Pipecat pipeline (STT -> LLM -> TTS)
- Basic inbound/outbound call flow
- FSM state machine
- Barge-in detection (VAD)
- Call session tracking

### Phase 3: Booking Core (Week 5-6)
- Slot generation from business_hours + services
- 3-layer booking defense (Redis + DB + Calendar)
- Google Calendar OAuth + integration
- Availability query endpoint
- Appointment CRUD with optimistic locking
- Reschedule flow

### Phase 4: Background Jobs (Week 7)
- APScheduler service (single instance)
- Calendar sync job
- Stale hold cleanup
- Reminder dispatch
- Retry pending bookings
- Job logging to job_logs table

### Phase 5: Polish (Week 8)
- Knowledge base + RAG integration
- Analytics dashboard endpoints
- Audit logging
- Call recording + transcript replay
- Admin dashboard (React)
- Load testing (100 concurrent calls)
- Documentation

---

## 12. Performance Targets

| Metric | Target |
|--------|--------|
| End-to-end latency | < 500ms |
| STT latency | < 200ms |
| LLM TTFT | < 300ms |
| TTS latency | < 100ms |
| Concurrent per worker | 10-20 per CPU core |
| Total concurrent | 100+ per worker node |
| Cost | $0 (self-hosted) |

---

## 13. File Structure

```
voice-agent-saas/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory
│   │   ├── config.py            # Settings, env vars
│   │   ├── dependencies.py      # DB, Redis, Auth deps
│   │   ├── middleware/
│   │   │   ├── tenant.py        # tenant_id extraction
│   │   │   ├── auth.py          # JWT validation
│   │   │   └── audit.py         # Audit logging
│   │   ├── api/v1/
│   │   │   ├── auth.py
│   │   │   ├── tenants.py
│   │   │   ├── users.py
│   │   │   ├── customers.py
│   │   │   ├── leads.py
│   │   │   ├── services.py
│   │   │   ├── slots.py
│   │   │   ├── appointments.py
│   │   │   ├── calls.py
│   │   │   ├── calendar.py
│   │   │   ├── ai_config.py
│   │   │   ├── voice_config.py
│   │   │   ├── knowledge.py
│   │   │   ├── notifications.py
│   │   │   ├── analytics.py
│   │   │   └── webhooks.py
│   │   ├── core/
│   │   │   ├── security.py
│   │   │   └── exceptions.py
│   │   ├── models/              # SQLAlchemy models
│   │   ├── services/
│   │   │   ├── tenant_service.py
│   │   │   ├── customer_service.py
│   │   │   ├── slot_service.py
│   │   │   ├── booking_service.py
│   │   │   ├── calendar_service.py
│   │   │   ├── call_service.py
│   │   │   ├── notification_service.py
│   │   │   └── knowledge_service.py
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py
│   │   │   └── migrations/
│   │   ├── scheduler/
│   │   │   └── jobs.py
│   │   ├── agents/
│   │   │   ├── pipeline.py
│   │   │   ├── fsm.py
│   │   │   ├── tools.py
│   │   │   └── worker.py
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
├── scheduler-service/
│   ├── Dockerfile
│   └── main.py
├── docker-compose.yml
└── README.md
```

---

## 14. Key Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Voice framework | Pipecat + LiveKit | Full pipeline control for complex FSM + multi-tenancy |
| No Celery | BackgroundTasks + APScheduler | Fewer moving parts. Voice workload does not need distributed queue. |
| Database pattern | Shared schema + RLS | Standard B2B SaaS. Simplest to maintain. |
| Soft delete | deleted_at on every table | Enterprise requirement. GDPR. Never hard-delete. |
| Slot table | Separate from appointments | Enables 3-layer concurrency defense. |
| Transcript | Structured JSONB events | Replayable, searchable, analytics-ready. |
| AI config | provider_config JSONB | Switch LLM providers without migration. |
| Audit logs | Append-only table | Compliance. Security. Enterprise sales. |
| Redis role | Cache + state only | Not a Celery broker. DB is source of truth. |

---

*Generated for Araaf (ArafathUIU) -- Production-Grade Voice Agent SaaS -- 2026*
