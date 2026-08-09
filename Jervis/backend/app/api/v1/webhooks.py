"""Twilio status-callback webhook.

Twilio POSTs call lifecycle events here (no-answer, busy, failed, completed,
...). We use it to record failed/unanswered attempts and rearm the retry queue.
Answered calls are finalized by the Media Streams WebSocket handler instead, so
this endpoint ignores ``answered``/``completed`` events for sessions it doesn't
know about — but it still records ``completed`` so a short call that never
opened a stream (or a machine greeting) doesn't leave the lead stuck in
'dialing'.
"""

import json
import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.config import settings
from app.db.session import async_session_factory
from app.services.call_service import CallService
from app.services.lead_service import LeadService
from app.services.voice_log import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio"])


@router.post("/status")
async def twilio_status_callback(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items()}
    logger.info("Twilio status callback: %s", data)

    # The TwiML <Parameter> values ride in the query string of the callback URL.
    lead_id = request.query_params.get("leadId")
    session_id = request.query_params.get("sessionId")
    call_status = data.get("CallStatus", "")
    call_sid = data.get("CallSid", "")
    log_event(
        "status_callback",
        lead_id=lead_id,
        session_id=session_id,
        call_sid=call_sid,
        call_status=call_status,
        duration=data.get("CallDuration"),
        sip_code=data.get("SipResponseCode"),
        body={k: v for k, v in data.items() if k in {
            "CallStatus", "CallSid", "CallDuration", "SipResponseCode",
            "Direction", "To", "From", "ErrorMessage", "ErrorCode",
        }},
    )

    if not session_id:
        logger.warning("Twilio status callback missing sessionId (status=%s)", call_status)
        return {"ok": True, "status": "ignored"}

    # We can't finalize an answered conversation here without knowing it actually
    # ran; that is owned by the WebSocket handler. But if the status is a
    # definitive non-answer, record it and rearm.
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        await _record_and_rearm(lead_id, session_id, call_status, call_sid, data)
        return {"ok": True, "status": "recorded", "outcome": call_status}

    if call_status in ("completed", "ringing", "initiated", "answered", "in-progress"):
        # Terminal 'completed' after an unanswered/unknown call: fall back to
        # marking it failed so the lead isn't stuck, unless the WS handler
        # already finalized the session.
        if call_status == "completed":
            await _maybe_mark_completed(lead_id, session_id, call_sid)
        return {"ok": True, "status": "noted", "call_status": call_status}

    return {"ok": True, "status": "ignored"}


async def _record_and_rearm(lead_id, session_id, outcome, call_sid, data):
    if not lead_id:
        return
    details = json.dumps({"call_sid": call_sid, "error": data.get("ErrorMessage")})
    async with async_session_factory() as db:
        svc = LeadService(db)
        cs = CallService(db)
        await svc.record_attempt(lead_id, outcome, session_id, details)
        await cs.end_session(session_id, outcome)
        await svc.rearm_retry(
            lead_id,
            retry_seconds=settings.outbound_retry_seconds,
            max_attempts=settings.outbound_max_attempts,
        )
        await db.commit()
    logger.info("Recorded %s attempt for lead=%s session=%s", outcome, lead_id, session_id)


async def _maybe_mark_completed(lead_id, session_id, call_sid):
    """Finalize a call that reached 'completed' but whose session wasn't ended."""
    if not lead_id:
        return
    async with async_session_factory() as db:
        cs = CallService(db)
        svc = LeadService(db)
        # If the WS handler already finalized it, the session is ended; skip.
        row = await db.execute(
            text("SELECT status FROM call_sessions WHERE id = :sid"),
            {"sid": session_id},
        )
        status = row.scalar_one_or_none()
        if status and status != "ended":
            await svc.record_attempt(
                lead_id, "completed", session_id, json.dumps({"call_sid": call_sid})
            )
            await cs.end_session(session_id, "completed")
            await svc.mark_completed(lead_id)
            await db.commit()
            logger.info("Finalized completed call lead=%s session=%s", lead_id, session_id)
