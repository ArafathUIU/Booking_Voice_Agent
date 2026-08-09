import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import settings
from app.db.session import async_session_factory
from app.services.twilio_client import create_outbound_call
from app.services.call_service import CallService
from app.services.lead_service import LeadService
from app.services.voice_log import log_event

logger = logging.getLogger(__name__)


async def start_outbound_call(lead_id: str) -> dict:
    """Orchestrate an outbound call for a lead.

    Atomically claims the lead (pending -> dialing) so the manual /outbound
    path and the poller can never dial the same lead twice — whichever runs
    second finds it already claimed and bails. Then creates the session and
    dials via Twilio Programmable Voice. The TwiML points Twilio's Media Stream
    at our /ws/twilio-media WebSocket, where the agent pipeline runs when the
    callee answers. Answer tracking + finalization happen in the WS handler and
    the status webhook, so we return once the call is placed.
    """
    async with async_session_factory() as db:
        svc = LeadService(db)
        cs = CallService(db)
        lead = await svc.claim_by_id(lead_id)
        if not lead:
            existing = await svc.get_lead(lead_id)
            logger.warning(
                "Lead %s not claimable (status=%s), skipping dial",
                lead_id, existing.get("status") if existing else "missing",
            )
            return {"status": "skipped", "reason": "not_claimable"}
        await db.commit()

        session = await cs.create_session(
            tenant_id=settings.tenant_id,
            room_name=f"twilio-{uuid.uuid4().hex[:12]}",
            session_type="phone_outbound",
            customer_name=lead["customer_name"],
            customer_phone=lead["phone"],
            lead_id=lead_id,
        )
        await db.commit()
        session_id = str(session.id)

        # Associate the lead with the live session.
        await db.execute(
            text(
                "UPDATE leads SET call_session_id=:sid, updated_at=:now WHERE id=:id"
            ),
            {
                "sid": uuid.UUID(session_id),
                "id": uuid.UUID(lead_id),
                "now": datetime.now(timezone.utc),
            },
        )
        await db.commit()

    try:
        call_sid = await create_outbound_call(
            settings.twilio_verified_number,
            lead_id=lead_id,
            session_id=session_id,
        )
        log_event(
            "call_placed",
            lead_id=lead_id,
            session_id=session_id,
            call_sid=call_sid,
            to=settings.twilio_verified_number,
            from_=settings.twilio_from_number,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Twilio dial failed for lead %s: %s", lead_id, e)
        log_event("dial_error", lead_id=lead_id, session_id=session_id, error=str(e))
        await _record(lead_id, session_id, "failed", f"dial_error: {e}")
        await _rearm_or_fail(lead_id, session_id)
        return {"status": "failed", "reason": "dial_error", "detail": str(e)}

    logger.info("Outbound call placed lead=%s session=%s call=%s", lead_id, session_id, call_sid)
    return {"status": "dialing", "session_id": session_id, "call_sid": call_sid}


async def _record(lead_id: str, session_id: str, outcome: str, details):
    async with async_session_factory() as db:
        svc = LeadService(db)
        await svc.record_attempt(lead_id, outcome, session_id, details)
        await db.commit()


async def _rearm_or_fail(lead_id: str, session_id: str):
    async with async_session_factory() as db:
        svc = LeadService(db)
        cs = CallService(db)
        await cs.end_session(session_id, "not_answered")
        await svc.rearm_retry(
            lead_id,
            retry_seconds=settings.outbound_retry_seconds,
            max_attempts=settings.outbound_max_attempts,
        )
        await db.commit()
