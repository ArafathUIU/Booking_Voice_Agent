"""Twilio voice endpoints: TwiML serving + the trial-native <Gather> loop.

Two outbound voice transports are supported, selected by
``TWILIO_VOICE_MODE``:

* ``media_streams`` (default off) — Twilio connects a Media Streams WebSocket
  here (via the ``<Connect><Stream>`` TwiML verb) when the callee answers, and
  the pipecat agent runs over the stream. Requires a paid/upgraded Twilio
  account; trial accounts strip the ``<Stream>`` verb.

* ``trial_native`` (default) — works on trial accounts. Twilio's built-in
  TTS (``<Say>``) and speech recognition (``<Gather input="speech">``) do the
  audio, and each spoken turn POSTs to ``/twiml/turn``, which runs the same
  deterministic booking brain (``TurnEngine``) as the streaming path.
"""

import asyncio
import json
import logging
from typing import Annotated
from xml.sax.saxutils import escape

from fastapi import APIRouter, Query, Request, Response, WebSocket

from app.agents import dialogue_manager as dm
from app.agents.constants import FALLBACK_SPEECH
from app.agents.session_registry import get as get_engine, pop as pop_engine, put as put_engine
from app.agents.turn_engine import (
    TurnEngine,
    build_outbound_greeting,
    finalize_session,
    persist_turn,
)
from app.agents.twilio_pipeline import build_and_run_twilio_pipeline
from app.config import settings
from app.db.session import async_session_factory
from app.services.call_service import CallService
from app.services.lead_service import LeadService
from app.services.twilio_client import _build_twiml
from app.services.voice_log import log_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["twilio"])

_START_TIMEOUT_S = 15
_XML_MEDIA_TYPE = "text/xml"


def _trial_native_url(path: str, lead_id: str, session_id: str) -> str:
    base = settings.twilio_public_base_url.rstrip("/")
    return f"{base}{path}?leadId={lead_id}&sessionId={session_id}"


def _gather_twiml(text: str, *, lead_id: str, session_id: str, hangup: bool = False) -> str:
    """Build the next turn's TwiML for the trial-native path.

    The ``action`` URL contains ``&`` query separators; they MUST be written as
    ``&amp;`` in the XML attribute or Twilio rejects the whole document and
    plays its "connection" error instead of our TwiML.
    """
    voice = escape(settings.twilio_say_voice or "Polly.Joanna")
    body = escape(text or FALLBACK_SPEECH)
    if hangup:
        return (
            "<Response>"
            f"<Say voice=\"{voice}\">{body}</Say>"
            "<Hangup/>"
            "</Response>"
        )
    action = escape(_trial_native_url("/twiml/turn", lead_id, session_id))
    return (
        "<Response>"
        f"<Gather input=\"speech dtmf\" timeout=\"5\" speechTimeout=\"auto\" "
        f"bargeIn=\"true\" numDigits=\"1\" action=\"{action}\" method=\"POST\">"
        f"<Say voice=\"{voice}\">{body}</Say>"
        "</Gather>"
        "</Response>"
    )


@router.api_route("/twiml", methods=["GET", "POST"])
async def twiml(
    request: Request,
    lead_id: Annotated[str, Query(alias="leadId")] = "",
    session_id: Annotated[str, Query(alias="sessionId")] = "",
) -> Response:
    """Serve the outbound-call TwiML at a public URL.

    ``media_streams`` mode returns the ``<Connect><Stream>`` TwiML the pipecat
    pipeline runs on. ``trial_native`` mode returns a ``<Gather>`` greeting so
    the call works without Media Streams (trial accounts strip ``<Stream>``).
    """
    if settings.twilio_voice_mode == "media_streams":
        return Response(content=_build_twiml(lead_id, session_id), media_type=_XML_MEDIA_TYPE)

    # trial_native: register a headless engine for this call and greet.
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

    engine = TurnEngine(
        session_id=session_id,
        tenant_id=settings.tenant_id,
        lead_context=lead_context,
    )
    put_engine(session_id, engine)
    greeting = build_outbound_greeting(engine.state, lead_context)
    log_event(
        "twiml_served",
        mode="trial_native",
        lead_id=lead_id,
        session_id=session_id,
        greeting=greeting,
        twiml=_gather_twiml(greeting, lead_id=lead_id, session_id=session_id),
    )
    logger.info("trial_native greeting for session=%s: %s", session_id, greeting)

    twiml_xml = _gather_twiml(greeting, lead_id=lead_id, session_id=session_id)
    return Response(content=twiml_xml, media_type=_XML_MEDIA_TYPE)


@router.post("/twiml/turn")
async def twiml_turn(
    request: Request,
    lead_id: Annotated[str, Query(alias="leadId")] = "",
    session_id: Annotated[str, Query(alias="sessionId")] = "",
):
    """Process one spoken (or DTMF) turn in trial_native mode.

    Twilio POSTs ``SpeechResult``/``Confidence``/``Digits`` here after each
    ``<Gather>``. We run the same deterministic booking brain as the streaming
    path, persist the turn, and return the next question — or hang up when the
    call closes.
    """
    form = await request.form()
    data = dict(form.items())
    speech = (data.get("SpeechResult") or "").strip()
    digits = (data.get("Digits") or "").strip()
    confidence = None
    try:
        confidence = float(data.get("Confidence"))
    except (TypeError, ValueError):
        pass
    call_sid = data.get("CallSid", "")
    logger.info(
        "twiml/turn session=%s speech=%r digits=%r confidence=%s call=%s",
        session_id, speech, digits, confidence, call_sid,
    )

    if not session_id:
        return Response(content=_gather_twiml("", lead_id="", session_id="", hangup=True),
                        media_type=_XML_MEDIA_TYPE)

    engine = get_engine(session_id)
    if engine is None:
        # Session wasn't initialised via /twiml (e.g. cached TwiML / restart).
        # Rebuild from the lead so the conversation can continue.
        lead_context = None
        async with async_session_factory() as db:
            svc = LeadService(db)
            lead = await svc.get_lead(lead_id) if lead_id else None
            if lead:
                lead_context = {
                    "customer_name": lead.get("customer_name"),
                    "customer_phone": lead.get("phone"),
                    "purpose": lead.get("purpose"),
                    "service": lead.get("service"),
                }
        engine = TurnEngine(
            session_id=session_id,
            tenant_id=settings.tenant_id,
            lead_context=lead_context,
        )
        put_engine(session_id, engine)
        greeting = build_outbound_greeting(engine.state, lead_context)
        logger.info("rebuilt engine session=%s greeting=%s", session_id, greeting)

    # DTMF fallback: a bare digit still answers the time/slot question.
    user_text = speech or digits

    result = await engine.process_turn(user_text, confidence)
    reply = result.text or "I'm sorry, could you say that again?"

    log_event(
        "turn",
        lead_id=lead_id,
        session_id=session_id,
        call_sid=call_sid,
        speech=speech,
        digits=digits,
        confidence=confidence,
        user_text=user_text,
        action=result.action,
        source=result.source,
        reply=reply,
        turn_no=result.turn_no,
        skipped=result.skipped,
        reply_twiml=_gather_twiml(reply, lead_id=lead_id, session_id=session_id),
    )

    await persist_turn(
        session_id,
        user_text=user_text,
        agent_text=reply,
        engine=engine,
    )

    if result.action == dm.CLOSE:
        pop_engine(session_id)
        await finalize_session(session_id, lead_id, "completed")
        return Response(
            content=_gather_twiml(reply, lead_id=lead_id, session_id=session_id, hangup=True),
            media_type=_XML_MEDIA_TYPE,
        )

    return Response(
        content=_gather_twiml(reply, lead_id=lead_id, session_id=session_id),
        media_type=_XML_MEDIA_TYPE,
    )


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
