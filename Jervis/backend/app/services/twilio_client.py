"""Twilio Programmable Voice REST client for outbound calls.

Unlike the LiveKit SIP path (blocked on Twilio trial), this dials a number
directly via the Twilio REST API and bridges audio back over a Media Streams
WebSocket. The caller is told to connect to our WebSocket endpoint via TwiML's
``<Connect><Stream>`` verb; our side hosts that endpoint (see twilio_ws.py) and
runs the same ConversationManager pipeline over it.
"""

import logging
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://api.twilio.com/2010-04-01"


def media_stream_url() -> str:
    """Public wss:// URL Twilio should stream audio to."""
    base = settings.twilio_public_base_url.rstrip("/")
    return f"wss://{urlparse(base).hostname}/ws/twilio-media"


def status_callback_url(lead_id: str, session_id: str) -> str:
    """Public https:// URL Twilio POSTs call-status events to."""
    return (
        f"{settings.twilio_public_base_url.rstrip('/')}/webhooks/twilio/status"
        f"?leadId={lead_id}&sessionId={session_id}"
    )


def twiml_url(lead_id: str, session_id: str) -> str:
    """Public https:// URL hosting the TwiML for an outbound call.

    Twilio trial accounts reject the inline ``Twiml`` parameter on
    ``Calls.create``, so the TwiML must be served from an HTTP URL instead.
    """
    return (
        f"{settings.twilio_public_base_url.rstrip('/')}/twiml"
        f"?leadId={lead_id}&sessionId={session_id}"
    )


def _build_twiml(lead_id: str, session_id: str) -> str:
    """TwiML that streams the live call audio to our WebSocket endpoint.

    ``<Parameter>`` values arrive in the WebSocket ``start`` message as
    ``customParameters``, letting the WS handler attach the stream to the
    correct session/lead.
    """
    return (
        "<Response>"
        f"<Connect><Stream url=\"{media_stream_url()}\">"
        f"<Parameter name=\"leadId\" value=\"{lead_id}\"/>"
        f"<Parameter name=\"sessionId\" value=\"{session_id}\"/>"
        "</Stream></Connect>"
        "</Response>"
    )


async def create_outbound_call(
    to_number: str,
    *,
    lead_id: str,
    session_id: str,
    ringing_timeout: int | None = None,
    max_call_duration: int | None = None,
) -> str:
    """Place an outbound call via Twilio REST. Returns the call SID.

    The TwiML is served from a public URL (``/twiml``) rather than inlined,
    because Twilio trial accounts reject the ``Twiml``/``Timeout``/``TimeLimit``
    parameters on ``Calls.create``. ``ringing_timeout`` and ``max_call_duration``
    are accepted for API compatibility but ignored on trial accounts.

    Raises on Twilio API errors so the caller can record a failed attempt.
    """
    import aiohttp

    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not configured")
    if not settings.twilio_from_number:
        raise RuntimeError("TWILIO_FROM_NUMBER not configured")

    params = {
        "To": to_number,
        "From": settings.twilio_from_number,
        # Trial accounts reject the inline `Twiml` param; serve it from a URL.
        "Url": twiml_url(lead_id, session_id),
        "StatusCallback": status_callback_url(lead_id, session_id),
        "StatusCallbackEvent": "initiated ringing answered completed",
    }

    url = f"{_API_BASE}/Accounts/{settings.twilio_account_sid}/Calls.json"
    auth = aiohttp.BasicAuth(settings.twilio_account_sid, settings.twilio_auth_token)

    async with aiohttp.ClientSession() as session:
        async with session.post(url, auth=auth, data=params) as resp:
            body = await resp.json()
            if resp.status not in (200, 201):
                code = body.get("code")
                message = body.get("message")
                logger.error(
                    "Twilio Calls.create failed status=%s code=%s message=%s to=%s",
                    resp.status, code, message, to_number,
                )
                raise RuntimeError(f"Twilio error {code}: {message}")
            call_sid = body.get("sid")
            logger.info(
                "Twilio call placed call_sid=%s to=%s from=%s",
                call_sid, to_number, settings.twilio_from_number,
            )
            return call_sid
