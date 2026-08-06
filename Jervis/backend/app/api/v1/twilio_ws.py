"""Twilio Media Streams WebSocket endpoint.

Twilio connects here (via the ``<Stream url="wss://.../ws/twilio-media">`` TwiML
verb in an outbound call) when the callee answers. The first message is a
``start`` event carrying ``streamSid`` and the ``customParameters`` we attached
in TwiML (leadId, sessionId). From then on this handler runs the standard agent
pipeline over the stream until the call ends.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request, Response, WebSocket

from app.agents.twilio_pipeline import build_and_run_twilio_pipeline
from app.config import settings
from app.db.session import async_session_factory
from app.services.call_service import CallService
from app.services.lead_service import LeadService
from app.services.twilio_client import _build_twiml

logger = logging.getLogger(__name__)

router = APIRouter(tags=["twilio"])

_START_TIMEOUT_S = 15


@router.api_route("/twiml", methods=["GET", "POST"])
async def twiml(
    request: Request,
    leadId: str = Query(default=""),
    sessionId: str = Query(default=""),
) -> Response:
    """Serve the outbound-call TwiML at a public URL.

    Twilio trial accounts reject the inline ``Twiml`` parameter on
    ``Calls.create``, so ``create_outbound_call`` points Twilio at this URL
    instead. The ``<Parameter>`` values ride in the query string and arrive
    back in the Media Streams WebSocket ``start`` event as ``customParameters``.
    """
    twiml_xml = _build_twiml(leadId, sessionId)
    return Response(content=twiml_xml, media_type="text/xml")


async def _read_start_message(websocket: WebSocket) -> dict | None:
    """Read messages until the ``start`` event arrives (or timeout)."""
    try:
        async with asyncio.timeout(_START_TIMEOUT_S):
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                if msg.get("event") == "start":
                    return msg
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read Twilio start message")
        return None


def _extract_params(start: dict) -> tuple[str, str, str, str]:
    stream_sid = start.get("streamSid", "")
    start_obj = start.get("start", {})
    call_sid = start_obj.get("callSid", "")
    custom = start_obj.get("customParameters", {})
    return stream_sid, call_sid, custom.get("leadId", ""), custom.get("sessionId", "")


@router.websocket("/ws/twilio-media")
async def twilio_media_stream(websocket: WebSocket):
    await websocket.accept()

    start = await _read_start_message(websocket)
    if not start:
        await websocket.close()
        return
    stream_sid, call_sid, lead_id, session_id = _extract_params(start)
    if not stream_sid or not session_id:
        logger.error("Twilio start missing streamSid/sessionId: %s", start)
        await websocket.close()
        return

    logger.info("Twilio stream start call=%s session=%s lead=%s", call_sid, session_id, lead_id)

    # Load the lead so the agent can greet by name / purpose, then mark answered.
    lead_context = None
    async with async_session_factory() as db:
        cs = CallService(db)
        svc = LeadService(db)
        lead = await svc.get_lead(lead_id) if lead_id else None
        if lead:
            lead_context = {
                "customer_name": lead.get("customer_name"),
                "customer_phone": lead.get("phone"),
                "purpose": lead.get("purpose"),
                "service": lead.get("service"),
            }
        if lead_id:
            await svc.record_attempt(lead_id, "answered", session_id, None)
        await cs.start_session(session_id)
        await db.commit()

    try:
        await build_and_run_twilio_pipeline(
            websocket,
            stream_sid=stream_sid,
            call_sid=call_sid,
            session_id=session_id,
            tenant_id=settings.tenant_id,
            lead_context=lead_context,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Twilio pipeline failed session=%s", session_id)

    # Call has ended; finalize the lead + session.
    async with async_session_factory() as db:
        cs = CallService(db)
        svc = LeadService(db)
        await cs.end_session(session_id, "completed")
        await svc.mark_completed(lead_id)
        await db.commit()
    logger.info("Twilio call finalized session=%s lead=%s", session_id, lead_id)
